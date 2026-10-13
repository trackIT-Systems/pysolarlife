"""Keeps one connection to the strongest SolarLife device.

``ConnectionManager.run()`` is the only task that scans or connects, so a scan and a
connect attempt can never overlap. While a scan runs, BlueZ rejects new connections
(``org.bluez.Error.InProgress``), and while the Wi-Fi access point is up, opening a
connection often fails, so failed attempts are retried with a jittered, growing delay.
A link that is already open is stable and is kept; it is only rescanned at a long
interval to see whether a clearly stronger device has appeared.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Callable, Mapping
from datetime import datetime
from enum import StrEnum
from typing import Any

from solarlife.ble import Advertisement, BleBackend
from solarlife.config import BleConfig
from solarlife.errors import ConnectError
from solarlife.protocol import MpptProtocol
from solarlife.reading import Event
from solarlife.session import DeviceSession
from solarlife.state import Stats

log = logging.getLogger(__name__)

FAILURE_PENALTY_DB = 5.0
ATTACH_TIMEOUT_S = 10.0
DISCONNECT_TIMEOUT_S = 10.0
SCAN_GRACE_S = 15.0


class ConnState(StrEnum):
    STARTING = "starting"
    SCANNING = "scanning"
    CONNECTING = "connecting"
    BACKOFF = "backoff"
    CONNECTED = "connected"
    RESCANNING = "rescanning"
    DISCONNECTING = "disconnecting"
    STOPPED = "stopped"


class _Stopping(Exception):
    """Raised inside run() to unwind when stop() was requested."""


class Backoff:
    """Full-jitter exponential backoff: delay = random() * min(maximum, initial * factor**n)."""

    def __init__(self, initial: float, maximum: float, factor: float = 2.0, rng: Callable[[], float] = random.random) -> None:
        self.initial = initial
        self.maximum = maximum
        self.factor = factor
        self._rng = rng
        self.attempt = 0

    def next(self, floor: float = 0.0) -> float:
        ceiling = min(self.maximum, self.initial * self.factor**self.attempt)
        self.attempt += 1
        return max(floor, self._rng() * ceiling)

    def reset(self) -> None:
        self.attempt = 0


def select_device(
    results: Mapping[str, Advertisement],
    current: Advertisement | None,
    current_rssi: float | None,
    pinned: str | None,
    hysteresis_db: float,
    failures: Mapping[str, int] | None = None,
) -> Advertisement | None:
    """Pick the device to use.

    Without a current device: the strongest one (minus a penalty per recent connect
    failure). With a current device: keep it unless another one is stronger by at least
    ``hysteresis_db``. A pinned address is the only candidate.
    """
    failures = failures or {}
    candidates = {a: adv for a, adv in results.items() if pinned is None or a == pinned}
    if not candidates:
        return None

    def score(adv: Advertisement) -> float:
        return adv.rssi - FAILURE_PENALTY_DB * failures.get(adv.address, 0)

    best = max(candidates.values(), key=score)
    if current is None:
        return best
    if current.address in candidates:
        fresh = candidates[current.address]
        current_score = score(fresh)
    else:
        # The device may stop advertising while connected; compare against what we saw before.
        fresh = current
        current_score = current_rssi if current_rssi is not None else current.rssi
    if best.address != current.address and score(best) >= current_score + hysteresis_db:
        return best
    return fresh


class ConnectionManager:
    def __init__(
        self,
        cfg: BleConfig,
        backend: BleBackend,
        emit_event: Callable[[Event], None],
        *,
        on_state: Callable[[ConnState], None] | None = None,
        on_switch: Callable[[], None] | None = None,
        rng: Callable[[], float] = random.random,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cfg = cfg
        self.backend = backend
        self._emit_event = emit_event
        self._on_state = on_state
        self._on_switch = on_switch
        self._clock = clock
        self._backoff = Backoff(cfg.backoff_initial_s, cfg.backoff_max_s, rng=rng)

        self.state = ConnState.STARTING
        self.device: Advertisement | None = None
        self.device_rssi: float | None = None
        self.session: DeviceSession | None = None
        self.connected = asyncio.Event()
        self.connected_since: datetime | None = None
        self.scan_results: dict[str, Advertisement] = {}
        self.last_scan_at: datetime | None = None
        self.last_scan_mono: float | None = None
        self.stats = Stats(started=datetime.now().astimezone())

        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._lost = False
        self._reconnect_reason: str | None = None
        self._rescan_waiters: list[asyncio.Future] = []
        self._failures: dict[str, int] = {}
        self._candidate: Advertisement | None = None
        self._retry_address: str | None = None  # the device we want a fresh handle for
        self._next_backoff_s: float | None = None
        self._connected_mono = 0.0
        self.next_rescan_mono: float | None = None  # for `status`
        self._last_address: str | None = None
        self._generation = 0  # callbacks of links we already dropped must not wake us

    # -- requests from the outside -----------------------------------------

    def request_reconnect(self, reason: str) -> None:
        """Drop the current link (if any) and connect again. ``cli`` skips the backoff."""
        self._reconnect_reason = reason
        self._wake.set()

    async def request_rescan(self) -> dict[str, Advertisement]:
        """Scan now. Returns the scan results once a scan that started after the request is done."""
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._rescan_waiters.append(future)
        self._wake.set()
        return await future

    def stop(self) -> None:
        """Ask run() to finish: it aborts a scan or connect, disconnects and returns."""
        self._stop.set()
        self._wake.set()

    # -- main loop ----------------------------------------------------------

    async def run(self) -> None:
        self._set_state(ConnState.SCANNING)
        try:
            while True:
                if self._stop.is_set():
                    break
                if self.session is None:
                    await self._acquire()
                else:
                    await self._hold()
        except _Stopping:
            pass
        finally:
            await self._teardown()
            self._set_state(ConnState.STOPPED)

    async def _acquire(self) -> None:
        """Scan and connect until a link is open."""
        while True:
            self._reconnect_reason = None
            if self._next_backoff_s is not None:
                delay, self._next_backoff_s = self._next_backoff_s, None
                self._set_state(ConnState.BACKOFF)
                await self._sleep(delay)

            adv = self._candidate
            stale = adv is not None and self._clock() - adv.seen_at > self.cfg.adv_max_age_s
            if adv is None or stale or self._rescan_waiters:
                # BlueZ forgets devices it does not see, and after a failed connect, so a handle
                # from an earlier scan cannot be trusted. If we only need the same device again,
                # the scan can end as soon as it is seen.
                want = None if self._rescan_waiters else self._retry_address
                results = await self._scan(ConnState.SCANNING, until_address=want)
                if want is not None and want in results:
                    adv = results[want]
                else:
                    self._retry_address = None
                    adv = select_device(
                        results, None, None, self.cfg.address, self.cfg.switch_hysteresis_db, self._failures
                    )
                self._candidate = adv
                if adv is None:
                    self._set_state(ConnState.SCANNING)
                    await self._sleep(self.cfg.search_interval_s)
                    continue
            if await self._try_connect(adv):
                return

    async def _try_connect(self, adv: Advertisement) -> bool:
        self._set_state(ConnState.CONNECTING)
        self.stats.connect_attempts += 1
        self._lost = False
        self._generation += 1
        generation = self._generation
        link = None
        try:
            link = await self._until_stop(
                asyncio.wait_for(
                    self.backend.connect(
                        adv, timeout_s=self.cfg.connect_timeout_s, on_disconnect=lambda: self._link_lost(generation)
                    ),
                    self.cfg.connect_timeout_s + SCAN_GRACE_S,
                )
            )
            session = DeviceSession(
                link,
                MpptProtocol(),
                request_timeout_s=self.cfg.request_timeout_s,
                attempts=self.cfg.request_attempts,
            )
            await self._until_stop(asyncio.wait_for(session.attach(), ATTACH_TIMEOUT_S))
            if self._lost:
                raise ConnectError("link lost while setting up notifications")
        except _Stopping:
            if link is not None:
                await self._disconnect_quietly(link)
            raise
        except Exception as exc:
            if link is not None:
                await self._disconnect_quietly(link)
            self._connect_failed(adv, exc)
            return False
        self._on_connected(adv, session)
        return True

    def _connect_failed(self, adv: Advertisement, exc: Exception) -> None:
        in_progress = isinstance(exc, ConnectError) and exc.in_progress
        not_found = isinstance(exc, ConnectError) and exc.not_found
        message = str(exc) or type(exc).__name__
        self.stats.last_error = message
        # Whatever went wrong, the handle from the scan may be gone from BlueZ: scan again
        # before the next attempt, but only for this device if it is still the one we want.
        self._candidate = None
        self._retry_address = adv.address
        if not_found:
            # Our handle was stale, not a radio problem: no backoff, no failure count.
            self._next_backoff_s = None
            log.info("connect to %s: BlueZ no longer knows the device, scanning again", adv.address)
            self._event("connect_failed", adv.address, attempt=self.stats.connect_attempts, error=message, retry_in_s=0)
            return
        failures = self._failures[adv.address] = self._failures.get(adv.address, 0) + 1
        floor = self.cfg.inprogress_min_s if in_progress else 0.0
        delay = self._next_backoff_s = self._backoff.next(floor)
        self.stats.backoff_s = delay
        if in_progress:
            log.warning("connect to %s: BlueZ is busy (another client scanning or connecting?)", adv.address)
        else:
            log.info("connect to %s failed (%d): %s", adv.address, failures, message)
        if failures >= self.cfg.max_connect_failures:
            # Probably unreachable from here: select again, with a penalty for this device.
            self._retry_address = None
        self._event("connect_failed", adv.address, attempt=self.stats.connect_attempts, error=message, retry_in_s=round(delay, 2))

    def _on_connected(self, adv: Advertisement, session: DeviceSession) -> None:
        if self._last_address is not None and self._last_address != adv.address and self._on_switch is not None:
            self._on_switch()
        self._last_address = adv.address
        self._failures[adv.address] = 0
        self.session = session
        self.device = adv
        self.device_rssi = adv.rssi
        self._candidate = adv
        self._retry_address = None
        self._connected_mono = self._clock()
        self.connected_since = datetime.now().astimezone()
        self.stats.connects += 1
        self.stats.backoff_s = None
        self.connected.set()
        self._set_state(ConnState.CONNECTED)
        self._event("connected", adv.address, rssi=round(adv.rssi), attempts=self.stats.connect_attempts, name=adv.name)

    async def _hold(self) -> None:
        """Stay connected; handle loss, reconnect requests and the periodic rescan."""
        session = self.session
        assert session is not None
        next_rescan = self.next_rescan_mono = self._clock() + self.cfg.rescan_interval_s
        while True:
            timed_out = await self._sleep(max(0.0, next_rescan - self._clock()))
            if self._lost or not session.link.is_connected:
                await self._drop("remote", backoff=True)
                return
            if self._reconnect_reason is not None:
                reason, self._reconnect_reason = self._reconnect_reason, None
                self._candidate = None
                await self._drop(reason, backoff=reason != "cli")
                return
            if timed_out or self._rescan_waiters:
                if await self._rescan_while_connected():
                    return
                next_rescan = self.next_rescan_mono = self._clock() + self.cfg.rescan_interval_s

    async def _rescan_while_connected(self) -> bool:
        """Scan with the link open. Returns True if we dropped it to switch devices."""
        assert self.device is not None
        self._set_state(ConnState.RESCANNING)
        results = await self._scan(ConnState.RESCANNING)
        best = select_device(
            results, self.device, self.device_rssi, self.cfg.address, self.cfg.switch_hysteresis_db, self._failures
        )
        if best is not None and best.address != self.device.address:
            self._event(
                "device_switched",
                best.address,
                **{"from": self.device.address, "rssi_from": self.device_rssi, "rssi_to": round(best.rssi)},
            )
            self._candidate = best
            await self._drop("switch", backoff=False)
            return True
        if best is not None and best.address == self.device.address and best.address in results:
            self.device_rssi = best.rssi
        self._set_state(ConnState.CONNECTED)
        return False

    async def _drop(self, reason: str, *, backoff: bool) -> None:
        """Close the current link and arrange for the next connect."""
        session, device = self.session, self.device
        self.next_rescan_mono = None
        up_s = self._clock() - self._connected_mono
        if backoff and up_s >= self.cfg.stable_after_s:
            self._backoff.reset()
        self._set_state(ConnState.DISCONNECTING)
        self._generation += 1
        if session is not None:
            await self._disconnect_quietly(session.link)
        self.session = None
        self.connected.clear()
        self.connected_since = None
        self.stats.disconnects += 1
        self._event("disconnected", device.address if device else None, reason=reason, up_s=round(up_s))
        # Whatever the reason, the old advertisement is stale by the time we connect again. Unless
        # we are switching away, the same device is the one we want back.
        if reason != "switch":
            self._candidate = None
            self._retry_address = device.address if device else None
        if backoff:
            delay = self._next_backoff_s = self._backoff.next()
            self.stats.backoff_s = delay
        else:
            self._next_backoff_s = None

    # -- scanning -----------------------------------------------------------

    async def _scan(self, state: ConnState, until_address: str | None = None) -> dict[str, Advertisement]:
        waiters, self._rescan_waiters = self._rescan_waiters, []
        self._set_state(state)
        duration = self.cfg.scan_duration_s
        results: dict[str, Advertisement] = {}
        try:
            results = await self._until_stop(
                asyncio.wait_for(self.backend.scan(duration, until_address=until_address), duration + SCAN_GRACE_S)
            )
        except _Stopping:
            for waiter in waiters:
                if not waiter.done():
                    waiter.set_result({})
            raise
        except Exception as exc:
            log.warning("scan failed: %s", exc)
            self.stats.last_error = f"scan: {exc}"
        else:
            if until_address is None:  # an early-ended scan is incomplete, do not report it as a survey
                self.scan_results = dict(results)
                self.last_scan_at = datetime.now().astimezone()
                self.last_scan_mono = self._clock()
            self._event("scan_completed", None, devices=len(results), **({"until": until_address} if until_address else {}))
        for waiter in waiters:
            if not waiter.done():
                waiter.set_result(dict(results))
        return results

    # -- helpers ------------------------------------------------------------

    def _link_lost(self, generation: int) -> None:
        """Callback from the backend when the device or BlueZ closed the link."""
        if generation != self._generation:
            return  # a link we already dropped
        self._lost = True
        if self.session is not None:
            self.session.on_disconnect()
        self._wake.set()

    async def _sleep(self, seconds: float) -> bool:
        """Wait until woken or the time is up. Returns True on timeout. Raises _Stopping on stop."""
        if self._stop.is_set():
            raise _Stopping
        timed_out = False
        try:
            await asyncio.wait_for(self._wake.wait(), seconds)
        except TimeoutError:
            timed_out = True
        self._wake.clear()
        if self._stop.is_set():
            raise _Stopping
        return timed_out

    async def _until_stop(self, awaitable):
        """Await something, but abort it (and raise _Stopping) as soon as stop() is called."""
        if self._stop.is_set():
            raise _Stopping
        task = asyncio.ensure_future(awaitable)
        stopper = asyncio.ensure_future(self._stop.wait())
        try:
            await asyncio.wait({task, stopper}, return_when=asyncio.FIRST_COMPLETED)
        except BaseException:
            task.cancel()
            stopper.cancel()
            raise
        if task.done():
            stopper.cancel()
            return task.result()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise _Stopping

    @staticmethod
    async def _disconnect_quietly(link) -> None:
        try:
            await asyncio.wait_for(link.disconnect(), DISCONNECT_TIMEOUT_S)
        except Exception as exc:
            log.debug("disconnect: %s", exc)

    async def _teardown(self) -> None:
        session = self.session
        self._generation += 1
        if session is not None:
            self._set_state(ConnState.DISCONNECTING)
            await self._disconnect_quietly(session.link)
            self._event("disconnected", self.device.address if self.device else None, reason="shutdown")
            self.stats.disconnects += 1
        self.session = None
        self.connected.clear()
        for waiter in self._rescan_waiters:
            if not waiter.done():
                waiter.set_result({})
        self._rescan_waiters = []

    def _set_state(self, state: ConnState) -> None:
        if state is self.state:
            return
        self.state = state
        log.info("state: %s", state.value)
        if self._on_state is not None:
            try:
                self._on_state(state)
            except Exception:
                log.exception("state callback failed")

    def _event(self, kind: str, device: str | None, **data: Any) -> None:
        try:
            self._emit_event(Event(datetime.now().astimezone(), kind, device, data))
        except Exception:
            log.exception("event callback failed")
