"""MQTT output: readings and events as CSV, JSON and CBOR, readings rate-limited.

* ``{host}/solarlife/{mac12}/{block}/{fmt}``: readings; at most one per device and
  block every ``publish_interval_s``, always the newest
* ``{host}/solarlife/events/{fmt}``: connection events, not rate-limited
* ``{host}/solarlife/state/{json,cbor}``: connection state as a map, retained; the
  will sets it to offline

See docs/outputs.md for the columns and encodings.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import threading
import time
from collections.abc import Callable
from typing import Any

import cbor2
import paho.mqtt.client as mqtt

from solarlife.clock import Clock
from solarlife.config import MqttConfig
from solarlife.outputs.formats import ENCODERS, EVENTS, Record, event_record, reading_record
from solarlife.reading import Event, Reading, mac12

log = logging.getLogger(__name__)

APP = "solarlife"

# A callable that runs `callback` after `delay` seconds, returning something with cancel()
Scheduler = Callable[[float, Callable[[], None]], Any]


def topic(hostname: str, name: str, fmt: str) -> str:
    return f"{hostname}/{APP}/{name}/{fmt}"


def make_client() -> mqtt.Client:
    return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)


def default_scheduler(delay: float, callback: Callable[[], None]) -> Any:
    """On the daemon's event loop when called from it, else on a timer thread."""
    try:
        return asyncio.get_running_loop().call_later(delay, callback)
    except RuntimeError:
        timer = threading.Timer(delay, callback)
        timer.daemon = True
        timer.start()
        return timer


class MqttOutput:
    def __init__(
        self,
        config: MqttConfig,
        hostname: str | None = None,
        client: Any | None = None,
        clock: Clock | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        scheduler: Scheduler = default_scheduler,
    ) -> None:
        self._qos = config.qos
        self._interval = config.publish_interval_s
        self._hostname = hostname or socket.gethostname()
        self._formats = [f for f in ENCODERS if getattr(config, f)]
        self._state_formats = [f for f in ("json", "cbor") if getattr(config, f)]
        self._clock = clock or Clock()
        self._monotonic = monotonic
        self._scheduler = scheduler
        self._lock = threading.RLock()
        # rate limit, per (device, block): when it was last published, the newest
        # reading held back since, and the flush scheduled for it
        self._last: dict[tuple[str, str], float] = {}
        self._pending: dict[tuple[str, str], Record] = {}
        self._timers: dict[tuple[str, str], Any] = {}
        self._client = client if client is not None else make_client()
        for fmt in self._state_formats:
            self._client.will_set(
                topic(self._hostname, "state", fmt), self._encode_map({"state": "offline"}, fmt), qos=self._qos, retain=True
            )
        # connect_async: the daemon must start even if the broker is not up yet.
        self._client.connect_async(config.host, config.port)
        self._client.loop_start()

    @staticmethod
    def _encode_map(payload: dict[str, Any], fmt: str) -> bytes:
        if fmt == "cbor":
            return cbor2.dumps(payload)
        return json.dumps(payload, default=str).encode("utf-8")

    def _publish(self, name: str, record: Record) -> None:
        for fmt in self._formats:
            self._client.publish(topic(self._hostname, name, fmt), ENCODERS[fmt](record), qos=self._qos, retain=False)

    @staticmethod
    def _name(key: tuple[str, str]) -> str:
        device, block = key
        return f"{mac12(device)}/{block}"

    def emit_reading(self, reading: Reading) -> None:
        record = reading_record(reading, self._clock.synced())
        key = (reading.device, reading.block)
        with self._lock:
            now = self._monotonic()
            last = self._last.get(key)
            if self._interval <= 0 or last is None or (now - last >= self._interval and key not in self._pending):
                self._last[key] = now
                self._publish(self._name(key), record)
                return
            # within the interval: keep only the newest, sent when the interval ends
            self._pending[key] = record
            if key not in self._timers:
                self._timers[key] = self._scheduler(max(last + self._interval - now, 0.0), lambda: self._flush(key))

    def _flush(self, key: tuple[str, str]) -> None:
        with self._lock:
            self._timers.pop(key, None)
            record = self._pending.pop(key, None)
            if record is None:
                return
            self._last[key] = self._monotonic()
            self._publish(self._name(key), record)

    def emit_event(self, event: Event) -> None:
        self._publish(EVENTS, event_record(event, self._clock.synced()))

    def emit_state(self, state: dict[str, Any]) -> None:
        for fmt in self._state_formats:
            self._client.publish(topic(self._hostname, "state", fmt), self._encode_map(state, fmt), qos=self._qos, retain=True)

    def close(self) -> None:
        # what is held back goes out before the connection closes
        with self._lock:
            for timer in self._timers.values():
                timer.cancel()
            self._timers.clear()
            for key in list(self._pending):
                self._publish(self._name(key), self._pending.pop(key))
        self._client.disconnect()
        self._client.loop_stop()
