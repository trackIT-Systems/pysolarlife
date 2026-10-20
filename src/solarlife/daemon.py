"""solarlifed: holds a BLE connection to the strongest SolarLife device and reports its values."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import socket
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from solarlife import __version__
from solarlife.ble import BleBackend
from solarlife.config import DEFAULT_CONFIG_PATH, AppConfig, load_config
from solarlife.errors import ConfigError, NotConnectedError, SolarlifeError
from solarlife.manager import ConnectionManager, ConnState
from solarlife.outputs import Output, Outputs
from solarlife.clock import Clock
from solarlife.outputs.csv import CsvOutput
from solarlife.outputs.mqtt import MqttOutput
from solarlife.poller import Poller
from solarlife.reading import Event, Value, local_iso, utc_iso
from solarlife.registers import BLOCKS, Block, blocks_touched, build_write, plan_writes
from solarlife.server import BadRequest, ControlServer
from solarlife.state import Cache

log = logging.getLogger("solarlifed")

RESCAN_GRACE_S = 30.0


def build_outputs(config: AppConfig, hostname: str | None = None) -> Outputs:
    sinks: list[Output] = []
    # One clock for both outputs, so they agree on when the clock got synced
    csv_output = None
    if config.daemon.csv.enabled:
        csv_output = CsvOutput(config.daemon.csv.path, hostname)
        log.info("csv files in %s", csv_output.dir)
        sinks.append(csv_output)
    if config.daemon.mqtt.enabled:
        sinks.append(MqttOutput(config.daemon.mqtt, hostname, clock=csv_output.clock if csv_output else Clock()))
    if not sinks:
        log.warning("no outputs enabled; readings are only available through the CLI")
    return Outputs(sinks)


def _block(name: Any) -> Block:
    if name not in BLOCKS:
        raise BadRequest(f"unknown block {name!r}, expected one of: {', '.join(BLOCKS)}, all")
    return BLOCKS[name]


def _blocks(args: dict[str, Any], default: str = "all") -> list[Block]:
    name = args.get("block", default)
    return list(BLOCKS.values()) if name == "all" else [_block(name)]


class Daemon:
    def __init__(
        self,
        config: AppConfig,
        *,
        backend: BleBackend | None = None,
        outputs: Outputs | None = None,
        hostname: str | None = None,
    ) -> None:
        self.config = config
        self.hostname = hostname or socket.gethostname()
        if backend is None:
            from solarlife.ble.bleak_backend import BleakBackend

            backend = BleakBackend(config.ble.adapter)
        self.outputs = outputs if outputs is not None else build_outputs(config, self.hostname)
        self.cache = Cache(config.polling)
        self.manager = ConnectionManager(
            config.ble,
            backend,
            self._on_event,
            on_state=self._on_state,
            on_switch=self.cache.clear,
        )
        self.poller = Poller(self.manager, self.cache, config.polling, self.outputs.emit_reading, self._on_event)
        self.server = ControlServer(config.daemon.socket, config.daemon.socket_mode, self._handlers())
        self.stop_event = asyncio.Event()
        self._started_mono = time.monotonic()
        self._state_since = datetime.now().astimezone()

    # -- events and state ---------------------------------------------------

    def _on_event(self, event: Event) -> None:
        self.outputs.emit_event(event)

    def _on_state(self, state: ConnState) -> None:
        self._state_since = datetime.now().astimezone()
        self.outputs.emit_state(self.state_payload())

    def state_payload(self) -> dict[str, Any]:
        device = self.manager.device if self.manager.session is not None else None
        return {
            "_time": local_iso(datetime.now().astimezone()),
            "state": self.manager.state.value,
            "device": device.address if device else None,
            "rssi": round(self.manager.device_rssi) if device and self.manager.device_rssi is not None else None,
            "since": utc_iso(self._state_since),
        }

    # -- lifecycle ----------------------------------------------------------

    async def run(self, *, handle_signals: bool = True) -> int:
        loop = asyncio.get_running_loop()
        if handle_signals:
            self._install_signal_handlers(loop)
        self._on_event(Event(datetime.now().astimezone(), "daemon_started", None, {"version": __version__}))
        await self.server.start()
        manager_task = asyncio.create_task(self.manager.run(), name="manager")
        poller_task = asyncio.create_task(self.poller.run(), name="poller")
        stopper = asyncio.create_task(self.stop_event.wait())
        exit_code = 0
        try:
            done, _ = await asyncio.wait({manager_task, poller_task, stopper}, return_when=asyncio.FIRST_COMPLETED)
            for task in done - {stopper}:
                log.error("task %s ended unexpectedly: %r", task.get_name(), task.exception() if not task.cancelled() else "cancelled")
                exit_code = 1
        finally:
            await self._shutdown(manager_task, poller_task, stopper)
        return exit_code

    def _install_signal_handlers(self, loop: asyncio.AbstractEventLoop) -> None:
        """Stop on SIGTERM and SIGINT. Windows event loops have no add_signal_handler: signal.signal then."""
        for name in ("SIGTERM", "SIGINT", "SIGBREAK"):  # SIGBREAK: Ctrl+Break on Windows
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            try:
                loop.add_signal_handler(sig, self.stop_event.set)
            except NotImplementedError:
                signal.signal(sig, lambda *_: loop.call_soon_threadsafe(self.stop_event.set))

    async def _shutdown(self, manager_task: asyncio.Task, poller_task: asyncio.Task, stopper: asyncio.Task) -> None:
        log.info("shutting down")
        stopper.cancel()
        await self.server.stop()
        poller_task.cancel()
        self.manager.stop()
        await asyncio.gather(manager_task, poller_task, return_exceptions=True)
        self._on_event(Event(datetime.now().astimezone(), "daemon_stopped"))
        self.outputs.emit_state({**self.state_payload(), "state": "offline", "device": None, "rssi": None})
        self.outputs.close()
        log.info("stopped")

    # -- commands -----------------------------------------------------------

    def _handlers(self) -> dict[str, Any]:
        return {
            "status": self._cmd_status,
            "cached": self._cmd_cached,
            "read": self._cmd_read,
            "write": self._cmd_write,
            "reconnect": self._cmd_reconnect,
            "rescan": self._cmd_rescan,
            "scan": self._cmd_scan,
        }

    def _device_info(self) -> dict[str, Any] | None:
        device = self.manager.device
        if device is None:
            return None
        return {
            "address": device.address,
            "name": device.name,
            "rssi": None if self.manager.device_rssi is None else round(self.manager.device_rssi),
            "pinned": self.config.ble.address is not None,
            "connected": self.manager.session is not None,
        }

    async def _cmd_status(self, args: dict[str, Any]) -> dict[str, Any]:
        manager, stats = self.manager, self.manager.stats
        now = time.monotonic()
        scan: dict[str, Any] = {
            "last": utc_iso(manager.last_scan_at) if manager.last_scan_at else None,
            "devices": len(manager.scan_results),
            "next_in_s": None,
        }
        if manager.next_rescan_mono is not None:
            scan["next_in_s"] = max(0, round(manager.next_rescan_mono - now))
        return {
            "version": __version__,
            "state": manager.state.value,
            "since": utc_iso(manager.connected_since if manager.state is ConnState.CONNECTED and manager.connected_since else self._state_since),
            "uptime_s": round(now - self._started_mono),
            "device": self._device_info(),
            "blocks": {name: self.cache.describe(name) for name in BLOCKS},
            "connection": {
                "connect_attempts": stats.connect_attempts,
                "connects": stats.connects,
                "disconnects": stats.disconnects,
                "last_error": stats.last_error,
                "backoff_s": None if stats.backoff_s is None else round(stats.backoff_s, 1),
            },
            "scan": scan,
            "unsolicited": manager.session.unsolicited if manager.session else 0,
        }

    async def _cmd_cached(self, args: dict[str, Any]) -> dict[str, Any]:
        blocks: dict[str, Any] = {}
        for block in _blocks(args):
            state = self.cache.blocks[block.name]
            description = self.cache.describe(block.name)
            blocks[block.name] = {
                "time": utc_iso(state.reading.time) if state.reading else None,
                "age_s": description["age_s"],
                "stale": description["stale"],
                "values": [v.as_dict() for v in state.reading.values] if state.reading else [],
            }
        return {
            "device": state_device(self.cache),
            "connected": self.manager.session is not None,
            "blocks": blocks,
        }

    async def _cmd_read(self, args: dict[str, Any]) -> dict[str, Any]:
        if "address" in args:
            return await self._read_range(args)
        readings = {}
        for block in _blocks(args):
            reading = await self.poller.read(block)
            readings[block.name] = {"time": utc_iso(reading.time), "values": [v.as_dict() for v in reading.values]}
        return {"device": self.manager.device.address if self.manager.device else None, "blocks": readings}

    async def _read_range(self, args: dict[str, Any]) -> dict[str, Any]:
        session = self.manager.session
        if session is None:
            raise NotConnectedError(f"no device connected (state={self.manager.state.value})")
        try:
            address = int(str(args["address"]), 0)
            count = int(args.get("count", 1))
        except (TypeError, ValueError):
            raise BadRequest("address and count must be numbers") from None
        results = await session.read_block(address, count)
        values = [Value.from_result(r).as_dict() for r in results]
        return {"device": self.manager.device.address if self.manager.device else None, "range": {"address": f"0x{address:04x}", "count": count}, "values": values}

    async def _cmd_write(self, args: dict[str, Any]) -> dict[str, Any]:
        values = args.get("values")
        if not isinstance(values, dict) or not values:
            raise BadRequest("args.values must be an object of name: value")
        session = self.manager.session
        if session is None:
            raise NotConnectedError(f"no device connected (state={self.manager.state.value})")
        results = build_write(values, force=bool(args.get("force")))
        for group in plan_writes(results):
            await session.write(group)
            log.info("wrote %s", ", ".join(f"{r.name}={r.value}" for r in group))
        readback: dict[str, Any] = {}
        errors: dict[str, str] = {}
        for block in blocks_touched(results):
            try:
                reading = await self.poller.read(block)
                readback[block.name] = [v.as_dict() for v in reading.values]
            except SolarlifeError as exc:
                errors[block.name] = str(exc)
        out: dict[str, Any] = {"written": [{"name": r.name, "value": r.value} for r in results], "readback": readback}
        if errors:
            out["readback_errors"] = errors
        return out

    async def _cmd_reconnect(self, args: dict[str, Any]) -> dict[str, Any]:
        self.manager.request_reconnect("cli")
        return {"requested": True}

    async def _cmd_rescan(self, args: dict[str, Any]) -> dict[str, Any]:
        timeout = self.config.ble.scan_duration_s + RESCAN_GRACE_S
        await asyncio.wait_for(self.manager.request_rescan(), timeout)
        return await self._cmd_scan(args)

    async def _cmd_scan(self, args: dict[str, Any]) -> dict[str, Any]:
        manager = self.manager
        current = manager.device.address if manager.device and manager.session else None
        devices = [
            {"address": a.address, "name": a.name, "rssi": round(a.rssi), "samples": a.samples, "current": a.address == current, "advertising": True}
            for a in sorted(manager.scan_results.values(), key=lambda a: a.rssi, reverse=True)
        ]
        if current is not None and all(d["address"] != current for d in devices):
            # A connected SolarLife stops advertising, so a scan cannot see it. Report it anyway,
            # with the signal strength it had when we connected.
            device = manager.device
            rssi = None if manager.device_rssi is None else round(manager.device_rssi)
            devices.append({"address": current, "name": device.name, "rssi": rssi, "samples": 0, "current": True, "advertising": False})
        return {"time": utc_iso(manager.last_scan_at) if manager.last_scan_at else None, "devices": devices}


def state_device(cache: Cache) -> str | None:
    for state in cache.blocks.values():
        if state.reading is not None:
            return state.reading.device
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="solarlifed", description="Hold a BLE connection to a SolarLife MPPT controller and report its values (CSV, MQTT).")
    parser.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--config", type=Path, default=None, help=f"config file (default: {DEFAULT_CONFIG_PATH})")
    parser.add_argument("-v", "--verbose", action="count", default=0, help="more log output (-v: DEBUG)")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    level = logging.DEBUG if args.verbose else getattr(logging, config.daemon.log_level)
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    try:
        return asyncio.run(Daemon(config).run())
    except (SolarlifeError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
