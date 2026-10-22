"""Reads the register blocks through the held connection on a schedule."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

from solarlife.config import PollingConfig
from solarlife.errors import NotConnectedError, SolarlifeError
from solarlife.reading import Event, Reading
from solarlife.registers import PARAMETERS, STATUS, Block
from solarlife.state import Cache

log = logging.getLogger(__name__)

CHECK_INTERVAL_S = 0.5  # how often to notice that the link changed while waiting


class Poller:
    """Polls ``status`` and ``parameters`` while a device is connected.

    Both blocks are read right after every (re)connect. Nothing is read while
    disconnected, missed polls are not made up. After ``reconnect_after_failures``
    failed reads in a row the link is considered dead and a reconnect is requested.
    """

    def __init__(
        self,
        manager: Any,
        cache: Cache,
        cfg: PollingConfig,
        on_reading: Callable[[Reading], None],
        on_event: Callable[[Event], None],
        *,
        clock: Callable[[], float] = time.monotonic,
        check_interval_s: float = CHECK_INTERVAL_S,
    ) -> None:
        self.manager = manager
        self.cache = cache
        self.cfg = cfg
        self._on_reading = on_reading
        self._on_event = on_event
        self._clock = clock
        self._check_interval_s = check_interval_s
        self._intervals = {STATUS.name: cfg.status_interval_s, PARAMETERS.name: cfg.parameters_interval_s}
        # Failed reads in a row, over all blocks. Any successful read means the link works.
        self.consecutive_failures = 0
        # The last error logged per block, so that a read failing on every poll is logged once
        self._read_errors: dict[str, str] = {}
        self._reconnect_logged = False

    async def read(self, block: Block) -> Reading:
        """Read one block now (also used for live reads from the CLI).

        Updates the cache and the outputs. Raises NotConnectedError, TransactionTimeout,
        ProtocolError, DeviceException or BusyError.
        """
        session = self.manager.session
        device = self.manager.device
        if session is None or device is None:
            raise NotConnectedError(f"no device connected (state={self.manager.state})")
        try:
            results = await session.read_block(block.address, block.count)
        except NotConnectedError:
            raise  # the manager deals with the lost link
        except SolarlifeError as exc:
            self.cache.fail(block.name, str(exc))
            self.consecutive_failures += 1
            self._read_failed(block, device.address, exc)
            self._on_event(Event(datetime.now().astimezone(), "read_failed", device.address, {"block": block.name, "error": str(exc)}))
            raise
        reading = Reading.from_results(device.address, block.name, datetime.now().astimezone(), results)
        self.consecutive_failures = 0
        self._read_succeeded(block, device.address)
        self.cache.update(reading)
        self._on_reading(reading)
        return reading

    def _read_failed(self, block: Block, address: str, exc: Exception) -> None:
        """Log a failed read when it starts or its kind of error changes, the repeats at DEBUG.

        The kind is the exception type: a timeout's text names the bytes received
        so far, which differ from poll to poll.
        """
        kind = type(exc).__name__
        if self._read_errors.get(block.name) != kind:
            log.warning("Cannot read %s from %s: %s: %s", block.name, address, kind, exc)
        else:
            log.debug("Still cannot read %s from %s: %s: %s", block.name, address, kind, exc)
        self._read_errors[block.name] = kind

    def _read_succeeded(self, block: Block, address: str) -> None:
        if self._read_errors.pop(block.name, None) is not None:
            log.info("Reading %s from %s works again", block.name, address)
        if not self._read_errors:
            self._reconnect_logged = False

    async def run(self) -> None:
        while True:
            await self.manager.connected.wait()
            session = self.manager.session
            if session is None:
                await asyncio.sleep(0)
                continue
            await self._poll_session(session)

    async def _poll_session(self, session: Any) -> None:
        due = {block.name: self._clock() for block in (STATUS, PARAMETERS)}
        self.consecutive_failures = 0
        reconnecting = False
        while self.manager.session is session and self.manager.connected.is_set():
            now = self._clock()
            if not reconnecting:
                for block in (STATUS, PARAMETERS):
                    if due[block.name] > now:
                        continue
                    due[block.name] = now + self._intervals[block.name]
                    try:
                        await self.read(block)
                    except NotConnectedError:
                        break
                    except SolarlifeError:
                        pass
                    if self.consecutive_failures >= self.cfg.reconnect_after_failures:
                        if not self._reconnect_logged:
                            log.warning("Reconnecting after %d failed reads in a row", self.consecutive_failures)
                            self._reconnect_logged = True
                        else:
                            log.debug("Reconnecting after %d failed reads in a row", self.consecutive_failures)
                        self.manager.request_reconnect("read_failures")
                        reconnecting = True
                        break
            wait = min(due.values()) - self._clock() if not reconnecting else self._check_interval_s
            await asyncio.sleep(max(0.0, min(wait, self._check_interval_s)))
