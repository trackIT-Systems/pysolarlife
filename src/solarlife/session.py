"""One held BLE link: framing, request/response transactions, retries."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from solarlife.ble import BleLink
from solarlife.crc import crc16
from solarlife.errors import (
    BusyError,
    DeviceException,
    InvalidWriteError,
    LinkError,
    NotConnectedError,
    ProtocolError,
    SolarlifeError,
    TransactionTimeout,
)
from solarlife.protocol import MpptProtocol, Result, ResultContainer

log = logging.getLogger(__name__)

READ_DEVICE_ID = 0xFE
EXCEPTION_FLAG = 0x80
WRITE_RESPONSE_LENGTH = 8
EXCEPTION_FRAME_LENGTH = 5


@dataclass
class _Pending:
    function_code: int
    start_address: int
    count: int | None  # register count for reads, None for writes
    future: asyncio.Future = field(default_factory=lambda: asyncio.get_running_loop().create_future())
    buffer: bytearray = field(default_factory=bytearray)


class DeviceSession:
    """Serializes requests on one link and turns notifications into responses.

    * Only one request is pending at a time.
    * Notifications that arrive while nothing is pending, or whose function code does
      not match the pending request, are counted and dropped.
    * A response is only ever parsed when it is complete. Partial data is never returned.
    """

    def __init__(
        self,
        link: BleLink,
        protocol: MpptProtocol | None = None,
        *,
        request_timeout_s: float = 2.0,
        attempts: int = 3,
        write_timeout_s: float = 5.0,
        lock_timeout_s: float = 30.0,
    ) -> None:
        self.link = link
        self.protocol = protocol or MpptProtocol()
        self.request_timeout_s = request_timeout_s
        self.attempts = attempts
        self.write_timeout_s = write_timeout_s
        self.lock_timeout_s = lock_timeout_s
        self.unsolicited = 0
        self.last_unsolicited_hex: str | None = None
        self._lock = asyncio.Lock()
        self._pending: _Pending | None = None

    async def attach(self) -> None:
        await self.link.start_notify(self._on_notify)

    # -- public API ---------------------------------------------------------

    async def read_block(self, address: int, count: int) -> ResultContainer:
        try:
            command = self.protocol.get_read_command(READ_DEVICE_ID, address, count)
        except Exception as exc:
            raise ProtocolError(f"invalid read range 0x{address:04x}+{count}: {exc}") from exc
        return await self._transact(command, address, count)

    async def write(self, results: list[Result]) -> ResultContainer:
        try:
            start, command = self.protocol.get_write_command(self.protocol.device_id, list(results))
        except Exception as exc:
            raise InvalidWriteError(str(exc)) from exc
        await self._transact(command, start, None)
        return ResultContainer(list(results))

    def on_disconnect(self) -> None:
        """Called when the link drops. Fails the pending request, if any."""
        pending = self._pending
        if pending is not None and not pending.future.done():
            pending.future.set_exception(NotConnectedError("link lost during request"))

    # -- transactions -------------------------------------------------------

    async def _transact(self, command: bytes, start: int, count: int | None) -> ResultContainer:
        try:
            await asyncio.wait_for(self._lock.acquire(), self.lock_timeout_s)
        except TimeoutError:
            raise BusyError("connection is busy with another request") from None
        try:
            return await self._transact_locked(command, start, count)
        finally:
            self._lock.release()

    async def _transact_locked(self, command: bytes, start: int, count: int | None) -> ResultContainer:
        function_code = command[1]
        last: SolarlifeError | None = None
        for attempt in range(1, self.attempts + 1):
            if not self.link.is_connected:
                raise NotConnectedError("not connected")
            pending = _Pending(function_code, start, count)
            self._pending = pending
            try:
                await asyncio.wait_for(self.link.write(command), self.write_timeout_s)
                return await asyncio.wait_for(pending.future, self.request_timeout_s)
            except TimeoutError:
                partial = bytes(pending.buffer).hex() or "nothing"
                last = TransactionTimeout(
                    f"no complete response to 0x{function_code:02x}@0x{start:04x} "
                    f"(attempt {attempt}/{self.attempts}, received: {partial})"
                )
            except (ProtocolError, LinkError) as exc:
                last = exc
            finally:
                self._pending = None
            log.debug("Request failed: %s", last)
        assert last is not None
        raise last

    # -- notifications ------------------------------------------------------

    def _on_notify(self, chunk: bytes) -> None:
        pending = self._pending
        if pending is None or pending.future.done():
            self._drop(chunk, "no request pending")
            return
        if not pending.buffer and (
            len(chunk) < 2 or chunk[1] not in (pending.function_code, pending.function_code | EXCEPTION_FLAG)
        ):
            self._drop(chunk, f"unexpected function code while waiting for 0x{pending.function_code:02x}")
            return
        pending.buffer += chunk
        try:
            result = self._complete(pending)
        except SolarlifeError as exc:
            pending.future.set_exception(exc)
            return
        if result is not None:
            pending.future.set_result(result)

    def _drop(self, chunk: bytes, why: str) -> None:
        self.unsolicited += 1
        self.last_unsolicited_hex = chunk.hex()
        log.debug("Dropping a notification (%s): %s", why, chunk.hex())

    def _complete(self, pending: _Pending) -> ResultContainer | None:
        """Return the parsed response once the buffer holds a full frame, else None."""
        buf = pending.buffer
        if len(buf) >= 2 and buf[1] == pending.function_code | EXCEPTION_FLAG:
            if len(buf) < EXCEPTION_FRAME_LENGTH:
                return None
            frame = bytes(buf[:EXCEPTION_FRAME_LENGTH])
            if crc16(frame[:3]) != frame[3:5]:
                raise ProtocolError(f"CRC mismatch in exception frame {frame.hex()}")
            raise DeviceException(frame[2])

        if pending.count is not None:
            if len(buf) >= 3 and buf[2] != 2 * pending.count:
                raise ProtocolError(f"expected {2 * pending.count} data bytes, response announces {buf[2]}")
            expected = 5 + 2 * pending.count
        else:
            expected = WRITE_RESPONSE_LENGTH
        if len(buf) < expected:
            return None

        frame = bytes(buf[:expected])
        if len(buf) > expected:
            log.debug("Ignoring %d extra byte(s) after the response: %s", len(buf) - expected, bytes(buf[expected:]).hex())
        try:
            results = self.protocol.parse(pending.start_address, frame)
        except Exception as exc:
            raise ProtocolError(str(exc)) from exc
        if pending.count is not None and not results:
            raise ProtocolError("response decoded to no values")
        return results
