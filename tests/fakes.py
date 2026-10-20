"""Fake BLE transport: a Modbus-style device behind the BleLink / BleBackend interfaces."""

from __future__ import annotations

import asyncio
import struct
import time
from collections.abc import Callable

from solarlife.ble import Advertisement
from solarlife.crc import crc16
from solarlife.errors import ConnectError, NotConnectedError

# Raw register words of a real status read (0x3030, 40 registers) plus one trailing zero.
STATUS_WORDS = [
    1, 0, 2400, 0, 32, 1, 2500, 2900, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    31, 2340, 0, 0, 0, 2340, 0, 0, 0, 580, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
]
# Raw register words of a real parameter block (0x9021, 10 registers) plus two zeros.
PARAMETER_WORDS = [0, 1060, 1180, 1440, 1470, 1360, 0, 1440, 1400, 0, 0, 0]


def free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def default_registers() -> dict[int, int]:
    registers = {0x3030 + i: word for i, word in enumerate(STATUS_WORDS)}
    registers.update({0x9021 + i: word for i, word in enumerate(PARAMETER_WORDS)})
    return registers


class FakeDevice:
    """Answers Modbus requests from a register map. Fault knobs are one-shot counters."""

    def __init__(self, registers: dict[int, int] | None = None, device_id: int = 0x01) -> None:
        self.registers = dict(default_registers() if registers is None else registers)
        self.device_id = device_id
        self.requests: list[bytes] = []
        self.drop_next = 0
        self.corrupt_crc_next = 0
        self.truncate_next = 0
        self.exception_code_next: int | None = None
        self.prelude: list[bytes] = []  # junk sent before the next response
        self.override: Callable[[bytes], bytes | None] | None = None

    def respond(self, request: bytes) -> list[bytes]:
        """Return the notifications to deliver for one request, in order."""
        self.requests.append(request)
        out: list[bytes] = []
        if self.prelude:
            out.extend(self.prelude)
            self.prelude = []
        if self.drop_next:
            self.drop_next -= 1
            return out
        frame = self._frame(request)
        if frame is None:
            return out
        if self.corrupt_crc_next:
            self.corrupt_crc_next -= 1
            frame = frame[:-1] + bytes([frame[-1] ^ 0xFF])
        if self.truncate_next:
            self.truncate_next -= 1
            frame = frame[:10]
        return out + [frame]

    def _frame(self, request: bytes) -> bytes | None:
        if self.override is not None:
            return self.override(request)
        if len(request) < 8 or crc16(request[:-2]) != request[-2:]:
            return None
        function_code = request[1]
        if self.exception_code_next is not None:
            code, self.exception_code_next = self.exception_code_next, None
            body = bytes([self.device_id, function_code | 0x80, code])
            return body + crc16(body)
        address = struct.unpack_from(">H", request, 2)[0]
        if function_code in (0x02, 0x03, 0x04):
            count = struct.unpack_from(">H", request, 4)[0]
            data = b"".join(struct.pack(">H", self.registers.get(address + i, 0)) for i in range(count))
            body = bytes([self.device_id, function_code, len(data)]) + data
            return body + crc16(body)
        if function_code in (0x05, 0x06):
            self.registers[address] = struct.unpack_from(">H", request, 4)[0]
            return request
        if function_code == 0x10:
            count = struct.unpack_from(">H", request, 4)[0]
            for i in range(count):
                self.registers[address + i] = struct.unpack_from(">H", request, 7 + 2 * i)[0]
            body = bytes([self.device_id, 0x10]) + request[2:6]
            return body + crc16(body)
        return None


class FakeLink:
    """A BleLink. Responses are delivered in 20-byte chunks through the event loop."""

    def __init__(self, address: str, device: FakeDevice, *, chunk_size: int = 20, latency_s: float = 0.001) -> None:
        self.address = address
        self.device = device
        self.chunk_size = chunk_size
        self.latency_s = latency_s
        self.connected = True
        self.writes: list[bytes] = []
        self.max_in_flight = 0
        self._in_flight = 0
        self._notify: Callable[[bytes], None] | None = None
        self._on_disconnect: Callable[[], None] | None = None
        self.disconnect_calls = 0

    @property
    def is_connected(self) -> bool:
        return self.connected

    async def start_notify(self, callback: Callable[[bytes], None]) -> None:
        self._notify = callback

    async def write(self, data: bytes) -> None:
        if not self.connected:
            raise NotConnectedError("fake link is disconnected")
        self.writes.append(bytes(data))
        loop = asyncio.get_running_loop()
        self._in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self._in_flight)
        delay = 0.0
        chunks: list[bytes] = []
        for frame in self.device.respond(bytes(data)):
            chunks.extend(frame[i : i + self.chunk_size] for i in range(0, len(frame), self.chunk_size))
        if not chunks:
            loop.call_later(self.latency_s, self._done)
        for index, chunk in enumerate(chunks):
            delay += self.latency_s
            loop.call_later(delay, self._deliver, chunk, index == len(chunks) - 1)

    def _done(self) -> None:
        self._in_flight -= 1

    def _deliver(self, chunk: bytes, last: bool) -> None:
        if self.connected and self._notify is not None:
            self._notify(chunk)
        if last:
            self._done()

    def inject(self, data: bytes) -> None:
        """Deliver an unsolicited notification right away."""
        assert self._notify is not None
        self._notify(data)

    def trigger_disconnect(self) -> None:
        """The device drops the link."""
        self.connected = False
        if self._on_disconnect is not None:
            self._on_disconnect()

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self.connected = False


class FakeBackend:
    """A BleBackend scripted with scan results and connect outcomes.

    ``connect()`` while a scan runs raises ConnectError(in_progress=True), as BlueZ does.
    """

    def __init__(self, devices: dict[str, FakeDevice] | None = None) -> None:
        self.devices: dict[str, FakeDevice] = devices if devices is not None else {}
        self.scan_results: list[dict[str, float]] = []  # one {address: rssi} per scan, last one repeats
        self.connect_script: list[Exception | None] = []  # per connect attempt; None = success
        self.scanning = False
        self.scan_delay_s = 0.0
        self.connect_delay_s = 0.0
        self.calls: list[tuple[str, float, str | None]] = []  # (kind, monotonic, address)
        self.links: list[FakeLink] = []
        self.violations: list[str] = []
        self._scans = 0

    async def scan(self, duration_s: float, *, until_address: str | None = None) -> dict[str, Advertisement]:
        self.calls.append(("scan", time.monotonic(), until_address))
        self.scanning = True
        try:
            await asyncio.sleep(self.scan_delay_s)
        finally:
            self.scanning = False
        index = min(self._scans, len(self.scan_results) - 1)
        self._scans += 1
        seen = self.scan_results[index] if self.scan_results else {}
        now = time.monotonic()
        return {
            address: Advertisement(address=address, name="SolarLife", rssi=rssi, samples=5, seen_at=now)
            for address, rssi in seen.items()
        }

    async def connect(self, adv: Advertisement, *, timeout_s: float, on_disconnect: Callable[[], None]) -> FakeLink:
        self.calls.append(("connect", time.monotonic(), adv.address))
        if self.scanning:
            self.violations.append("connect during scan")
            raise ConnectError("Operation already in progress", in_progress=True)
        await asyncio.sleep(self.connect_delay_s)
        outcome = self.connect_script.pop(0) if self.connect_script else None
        if outcome is not None:
            raise outcome
        device = self.devices.setdefault(adv.address, FakeDevice())
        link = FakeLink(adv.address, device)
        link._on_disconnect = on_disconnect
        self.links.append(link)
        return link


class FakeMqtt:
    def __init__(self):
        self.published = []
        self.wills = []
        self.connected_to = None
        self.looping = False

    def will_set(self, topic, payload, qos=0, retain=False):
        self.wills.append((topic, payload, retain))

    def connect_async(self, host, port):
        self.connected_to = (host, port)

    def loop_start(self):
        self.looping = True

    def loop_stop(self):
        self.looping = False

    def disconnect(self):
        self.connected_to = None

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, retain))


class FakeClock:
    """Wall clock and boot clock that only move when told to (solarlife.clock.Clock's interface).

    `time` starts wrong (as on a station without RTC) until `sync()` sets it.
    """

    def __init__(self, time: float = 0.0, boottime: float = 100.0, boot_id: str = "boot-a", synced: bool = False):
        self._time = time
        self._boottime = boottime
        self._boot_id = boot_id
        self._synced = synced

    def time(self) -> float:
        return self._time

    def boottime(self) -> float:
        return self._boottime

    def boot_id(self) -> str:
        return self._boot_id

    def synced(self) -> bool:
        return self._synced

    def advance(self, seconds: float) -> None:
        self._time += seconds
        self._boottime += seconds

    def sync(self, time: float) -> None:
        """chrony steps the wall clock; the boot clock is unaffected."""
        self._time = time
        self._synced = True


class FakeScheduler:
    """Collects scheduled callbacks (MqttOutput's flush) to run them when a test says so."""

    def __init__(self):
        self.calls = []

    def __call__(self, delay, callback):
        handle = FakeHandle(delay, callback)
        self.calls.append(handle)
        return handle

    def run_due(self):
        due, self.calls = [h for h in self.calls if not h.cancelled], []
        for handle in due:
            handle.callback()


class FakeHandle:
    def __init__(self, delay, callback):
        self.delay = delay
        self.callback = callback
        self.cancelled = False

    def cancel(self):
        self.cancelled = True
