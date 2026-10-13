import asyncio
import time
from datetime import datetime

import pytest

from fakes import FakeDevice, FakeLink
from solarlife.ble import Advertisement
from solarlife.config import PollingConfig
from solarlife.errors import NotConnectedError, TransactionTimeout
from solarlife.poller import Poller
from solarlife.registers import PARAMETERS, STATUS
from solarlife.session import DeviceSession
from solarlife.state import Cache

MAC = "AA:BB:CC:DD:EE:FF"


class StubManager:
    """Just the parts of ConnectionManager the poller uses."""

    def __init__(self):
        self.connected = asyncio.Event()
        self.session = None
        self.device = Advertisement(MAC, "SolarLife", -60, 5, time.monotonic())
        self.state = "connected"
        self.reconnects = []

    def request_reconnect(self, reason):
        self.reconnects.append(reason)

    async def connect(self, device=None, **session_kwargs):
        device = device or FakeDevice()
        link = FakeLink(MAC, device)
        session = DeviceSession(link, request_timeout_s=0.05, attempts=1, **session_kwargs)
        await session.attach()
        self.session = session
        self.connected.set()
        return device, link, session

    def disconnect(self):
        self.session = None
        self.connected.clear()


class Harness:
    def __init__(self, **polling):
        cfg = PollingConfig(**polling)
        self.manager = StubManager()
        self.cache = Cache(cfg)
        self.readings = []
        self.events = []
        self.poller = Poller(self.manager, self.cache, cfg, self.readings.append, self.events.append, check_interval_s=0.01)
        self.task = None

    async def __aenter__(self):
        self.task = asyncio.create_task(self.poller.run())
        return self

    async def __aexit__(self, *exc):
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)

    def blocks(self):
        return [r.block for r in self.readings]


async def until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.005)


def run(coro):
    return asyncio.run(coro)


def test_both_blocks_are_read_right_after_connecting():
    async def go():
        async with Harness(status_interval_s=60, parameters_interval_s=3600) as h:
            await asyncio.sleep(0.05)
            assert h.readings == []  # nothing before a connection exists
            await h.manager.connect()
            await until(lambda: len(h.readings) == 2)
            assert h.blocks() == ["status", "parameters"]
            assert len(h.readings[0].values) == 44 and len(h.readings[1].values) == 12
            assert h.readings[0].device == MAC
            assert not h.cache.is_stale("status") and not h.cache.is_stale("parameters")
            assert h.cache.blocks["status"].reading is h.readings[0]

    run(go())


def test_intervals_are_respected():
    async def go():
        async with Harness(status_interval_s=0.05, parameters_interval_s=30) as h:
            await h.manager.connect()
            await asyncio.sleep(0.32)
            assert h.blocks().count("status") >= 4
            assert h.blocks().count("parameters") == 1

    run(go())


def test_nothing_is_read_while_disconnected_and_reading_resumes_after_reconnect():
    async def go():
        async with Harness(status_interval_s=0.03, parameters_interval_s=30) as h:
            device, link, _ = await h.manager.connect()
            await until(lambda: len(h.readings) >= 3)
            h.manager.disconnect()
            await asyncio.sleep(0.05)
            count = len(h.readings)
            await asyncio.sleep(0.15)
            assert len(h.readings) == count and len(link.writes) == count
            await h.manager.connect()
            await until(lambda: len(h.readings) >= count + 2)
            assert h.blocks()[count:][:2] == ["status", "parameters"]  # both again, immediately

    run(go())


def test_repeated_failures_request_a_reconnect_once():
    async def go():
        device = FakeDevice()
        device.drop_next = 1000
        async with Harness(status_interval_s=0.02, parameters_interval_s=0.02, reconnect_after_failures=3) as h:
            await h.manager.connect(device)
            await until(lambda: h.manager.reconnects)
            await asyncio.sleep(0.1)
            assert h.manager.reconnects == ["read_failures"]
            assert h.readings == []
            assert [e.kind for e in h.events][0] == "read_failed"
            assert h.poller.consecutive_failures >= 3 and h.cache.failures() >= 2
            assert "no complete response" in h.cache.blocks["status"].last_error

    run(go())


def test_one_failed_read_is_not_enough_and_success_resets_the_count():
    async def go():
        device = FakeDevice()
        async with Harness(status_interval_s=0.03, parameters_interval_s=30, reconnect_after_failures=3) as h:
            device.drop_next = 2
            await h.manager.connect(device)
            await until(lambda: len(h.readings) >= 3)
            assert h.manager.reconnects == []
            assert h.poller.consecutive_failures == 0

    run(go())


def test_a_lost_link_is_not_counted_as_a_failed_read():
    async def go():
        device = FakeDevice()
        device.drop_next = 5
        async with Harness(status_interval_s=0.02, parameters_interval_s=30, reconnect_after_failures=2) as h:
            _, link, session = await h.manager.connect(device)
            await asyncio.sleep(0.01)
            link.connected = False
            session.on_disconnect()
            await asyncio.sleep(0.1)
            assert h.manager.reconnects == []

    run(go())


def test_read_raises_when_not_connected_and_updates_the_cache_when_connected():
    async def go():
        h = Harness()
        with pytest.raises(NotConnectedError, match="no device connected"):
            await h.poller.read(STATUS)
        await h.manager.connect()
        reading = await h.poller.read(PARAMETERS)
        assert len(reading.values) == 12
        assert h.readings == [reading] and h.cache.blocks["parameters"].reading is reading

    run(go())


def test_a_failed_live_read_is_recorded_and_raised():
    async def go():
        h = Harness()
        device, _, _ = await h.manager.connect()
        device.drop_next = 5
        with pytest.raises(TransactionTimeout):
            await h.poller.read(STATUS)
        assert h.cache.blocks["status"].consecutive_failures == 1
        assert h.events[0].kind == "read_failed" and h.events[0].data["block"] == "status"

    run(go())
