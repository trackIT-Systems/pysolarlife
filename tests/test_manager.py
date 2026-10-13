import asyncio
import time

import pytest

from fakes import FakeBackend
from solarlife.ble import Advertisement
from solarlife.config import BleConfig
from solarlife.errors import ConnectError
from solarlife.manager import Backoff, ConnectionManager, ConnState, select_device

A, B, C = "AA:00:00:00:00:01", "BB:00:00:00:00:02", "CC:00:00:00:00:03"


def adv(address, rssi, seen_at=None):
    return Advertisement(address, "SolarLife", rssi, 5, time.monotonic() if seen_at is None else seen_at)


def results(**rssis):
    return {a: adv(a, r) for a, r in rssis.items()}


# -- pure logic ---------------------------------------------------------------


def test_select_picks_the_strongest():
    res = {A: adv(A, -80), B: adv(B, -55), C: adv(C, -70)}
    assert select_device(res, None, None, None, 10).address == B


def test_select_nothing():
    assert select_device({}, None, None, None, 10) is None


def test_select_pinned_ignores_stronger_devices():
    res = {A: adv(A, -80), B: adv(B, -40)}
    assert select_device(res, None, None, A, 10).address == A
    assert select_device({B: adv(B, -40)}, None, None, A, 10) is None


def test_select_hysteresis_keeps_current_unless_clearly_weaker():
    current = adv(A, -70)
    assert select_device({A: adv(A, -70), B: adv(B, -62)}, current, -70, None, 10).address == A
    assert select_device({A: adv(A, -70), B: adv(B, -60)}, current, -70, None, 10).address == B


def test_select_uses_the_recorded_rssi_when_the_current_device_is_not_seen():
    current = adv(A, -60)
    assert select_device({B: adv(B, -55)}, current, -60, None, 10).address == A
    assert select_device({B: adv(B, -45)}, current, -60, None, 10).address == B


def test_select_penalizes_recent_failures():
    res = {A: adv(A, -50), B: adv(B, -53)}
    assert select_device(res, None, None, None, 10, {A: 1}).address == B


def test_backoff_grows_is_capped_and_resets():
    b = Backoff(2, 10, rng=lambda: 1.0)
    assert [b.next() for _ in range(5)] == [2, 4, 8, 10, 10]
    b.reset()
    assert b.next() == 2


def test_backoff_jitter_and_floor():
    b = Backoff(2, 10, rng=lambda: 0.5)
    assert b.next() == 1.0
    assert Backoff(2, 10, rng=lambda: 0.0).next(floor=5) == 5


# -- state machine ------------------------------------------------------------


def cfg(**kw):
    base = dict(
        scan_duration_s=0.01,
        search_interval_s=0.03,
        rescan_interval_s=3600.0,
        switch_hysteresis_db=10.0,
        adv_max_age_s=5.0,
        connect_timeout_s=1.0,
        backoff_initial_s=0.01,
        backoff_max_s=0.04,
        inprogress_min_s=0.0,
        stable_after_s=0.2,
        request_timeout_s=0.2,
    )
    base.update(kw)
    return BleConfig(**base)


class Harness:
    def __init__(self, backend, **kw):
        self.backend = backend
        self.events = []
        self.states = []
        self.switches = 0
        self.manager = ConnectionManager(
            cfg(**kw),
            backend,
            self.events.append,
            on_state=self.states.append,
            on_switch=self._switched,
            rng=lambda: 1.0,
        )
        self.task = None

    def _switched(self):
        self.switches += 1

    def kinds(self):
        return [e.kind for e in self.events]

    async def __aenter__(self):
        self.task = asyncio.create_task(self.manager.run())
        return self

    async def __aexit__(self, *exc):
        self.manager.stop()
        await asyncio.wait_for(self.task, 3)
        assert not self.backend.violations, self.backend.violations


async def until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.005)


def run(coro):
    return asyncio.run(coro)


def test_connects_to_the_strongest_device():
    async def go():
        backend = FakeBackend()
        backend.scan_results = [{A: -80, B: -55, C: -70}]
        async with Harness(backend) as h:
            await until(lambda: h.manager.connected.is_set())
            assert h.manager.device.address == B
            assert h.manager.state is ConnState.CONNECTED
            assert h.manager.session is not None
            assert [e.kind for e in h.events if e.kind != "scan_completed"] == ["connected"]
            assert h.events[-1].data["rssi"] == -55
        assert h.manager.state is ConnState.STOPPED
        assert backend.links[0].disconnect_calls == 1
        assert not h.manager.connected.is_set()

    run(go())


def test_keeps_scanning_until_a_device_appears():
    async def go():
        backend = FakeBackend()
        backend.scan_results = [{}, {}, {A: -60}]
        async with Harness(backend) as h:
            await until(lambda: h.manager.connected.is_set())
            assert len([c for c in backend.calls if c[0] == "scan"]) == 3

    run(go())


def test_failed_connects_are_retried_with_backoff():
    async def go():
        backend = FakeBackend()
        backend.scan_results = [{A: -60}]
        backend.connect_script = [ConnectError("page timeout"), ConnectError("page timeout"), None]
        async with Harness(backend) as h:
            await until(lambda: h.manager.connected.is_set())
            failed = [e for e in h.events if e.kind == "connect_failed"]
            assert len(failed) == 2
            assert failed[0].data["error"] == "page timeout"
            assert failed[0].data["retry_in_s"] == 0.01 and failed[1].data["retry_in_s"] == 0.02
            assert h.manager.stats.connect_attempts == 3 and h.manager.stats.connects == 1
            assert ConnState.BACKOFF in h.states

    run(go())


def test_in_progress_errors_wait_at_least_the_floor():
    async def go():
        backend = FakeBackend()
        backend.scan_results = [{A: -60}]
        backend.connect_script = [ConnectError("busy", in_progress=True), None]
        async with Harness(backend, inprogress_min_s=0.15) as h:
            await until(lambda: h.manager.connected.is_set())
            connects = [t for kind, t, _ in backend.calls if kind == "connect"]
            assert connects[1] - connects[0] >= 0.15

    run(go())


def test_a_stale_advertisement_is_rescanned_before_connecting():
    async def go():
        backend = FakeBackend()
        backend.scan_results = [{A: -60}]
        backend.connect_script = [ConnectError("timeout"), None]
        async with Harness(backend, adv_max_age_s=0.001, backoff_initial_s=0.05, backoff_max_s=0.05) as h:
            await until(lambda: h.manager.connected.is_set())
            kinds = [c[0] for c in backend.calls]
            assert kinds == ["scan", "connect", "scan", "connect"]

    run(go())


def test_device_not_found_triggers_a_new_scan():
    async def go():
        backend = FakeBackend()
        backend.scan_results = [{A: -60}]
        backend.connect_script = [ConnectError("gone", not_found=True), None]
        async with Harness(backend, adv_max_age_s=60) as h:
            await until(lambda: h.manager.connected.is_set())
            assert [c[0] for c in backend.calls] == ["scan", "connect", "scan", "connect"]

    run(go())


def test_too_many_failures_trigger_a_new_selection():
    async def go():
        backend = FakeBackend()
        backend.scan_results = [{A: -50, B: -53}]
        backend.connect_script = [ConnectError("x"), None]
        async with Harness(backend, adv_max_age_s=60, max_connect_failures=1) as h:
            await until(lambda: h.manager.connected.is_set())
            # A failed once and is penalized, so B is selected by the second scan.
            assert h.manager.device.address == B

    run(go())


def test_remote_disconnect_leads_to_a_reconnect():
    async def go():
        backend = FakeBackend()
        backend.scan_results = [{A: -60}]
        async with Harness(backend) as h:
            await until(lambda: h.manager.connected.is_set())
            first = backend.links[0]
            first.trigger_disconnect()
            await until(lambda: len(backend.links) == 2 and h.manager.connected.is_set())
            assert "disconnected" in h.kinds()
            gone = [e for e in h.events if e.kind == "disconnected"][0]
            assert gone.data["reason"] == "remote"
            assert h.manager.stats.disconnects == 1 and h.manager.stats.connects == 2

    run(go())


def test_backoff_resets_only_after_a_stable_connection():
    async def go():
        backend = FakeBackend()
        backend.scan_results = [{A: -60}]
        async with Harness(backend, stable_after_s=0.15) as h:
            await until(lambda: h.manager.connected.is_set())
            backend.links[0].trigger_disconnect()  # up for a few ms: flapping
            await until(lambda: len(backend.links) == 2 and h.manager.connected.is_set())
            assert h.manager._backoff.attempt == 1
            await asyncio.sleep(0.2)  # now stable
            backend.links[1].trigger_disconnect()
            await until(lambda: len(backend.links) == 3 and h.manager.connected.is_set())
            assert h.manager._backoff.attempt == 1  # reset to 0 on drop, then one delay was drawn

    run(go())


def test_cli_reconnect_skips_the_backoff():
    async def go():
        backend = FakeBackend()
        backend.scan_results = [{A: -60}]
        async with Harness(backend, backoff_initial_s=5, backoff_max_s=5) as h:
            await until(lambda: h.manager.connected.is_set())
            h.manager.request_reconnect("cli")
            await until(lambda: len(backend.links) == 2 and h.manager.connected.is_set(), timeout=1.5)
            assert backend.links[0].disconnect_calls == 1
            assert [e.data["reason"] for e in h.events if e.kind == "disconnected"] == ["cli"]

    run(go())


def test_rescan_keeps_the_device_below_the_hysteresis():
    async def go():
        backend = FakeBackend()
        backend.scan_results = [{A: -70}, {A: -70, B: -62}]
        async with Harness(backend) as h:
            await until(lambda: h.manager.connected.is_set())
            found = await asyncio.wait_for(h.manager.request_rescan(), 2)
            assert set(found) == {A, B}
            await until(lambda: h.manager.state is ConnState.CONNECTED)
            assert h.manager.device.address == A and len(backend.links) == 1 and h.switches == 0

    run(go())


def test_rescan_switches_to_a_clearly_stronger_device():
    async def go():
        backend = FakeBackend()
        backend.scan_results = [{A: -70}, {A: -70, B: -50}]
        async with Harness(backend) as h:
            await until(lambda: h.manager.connected.is_set())
            await asyncio.wait_for(h.manager.request_rescan(), 2)
            await until(lambda: len(backend.links) == 2 and h.manager.connected.is_set())
            assert h.manager.device.address == B
            assert backend.links[0].disconnect_calls == 1
            assert h.switches == 1
            assert "device_switched" in h.kinds()
            assert [e.data["reason"] for e in h.events if e.kind == "disconnected"] == ["switch"]

    run(go())


def test_pinned_address_is_never_left():
    async def go():
        backend = FakeBackend()
        backend.scan_results = [{A: -90, B: -40}]
        async with Harness(backend, address=A) as h:
            await until(lambda: h.manager.connected.is_set())
            await asyncio.wait_for(h.manager.request_rescan(), 2)
            await asyncio.sleep(0.05)
            assert h.manager.device.address == A and len(backend.links) == 1

    run(go())


def test_periodic_rescan_runs_while_connected_without_overlapping_a_connect():
    async def go():
        backend = FakeBackend()
        backend.scan_results = [{A: -60}]
        backend.scan_delay_s = 0.02
        async with Harness(backend, rescan_interval_s=0.05) as h:
            await until(lambda: h.manager.connected.is_set())
            await until(lambda: len([c for c in backend.calls if c[0] == "scan"]) >= 4)
            assert len(backend.links) == 1 and h.manager.state in (ConnState.CONNECTED, ConnState.RESCANNING)

    run(go())


def test_stop_aborts_a_connect_in_progress():
    async def go():
        backend = FakeBackend()
        backend.scan_results = [{A: -60}]
        backend.connect_delay_s = 30
        h = Harness(backend)
        async with h:
            await until(lambda: any(c[0] == "connect" for c in backend.calls))
            started = time.monotonic()
        assert time.monotonic() - started < 1
        assert h.manager.state is ConnState.STOPPED

    run(go())


def test_stop_aborts_a_scan_and_releases_rescan_waiters():
    async def go():
        backend = FakeBackend()
        backend.scan_results = [{A: -60}]
        backend.scan_delay_s = 30
        h = Harness(backend)
        async with h:
            await until(lambda: any(c[0] == "scan" for c in backend.calls))
            waiter = asyncio.create_task(h.manager.request_rescan())
            await asyncio.sleep(0.01)
        assert await asyncio.wait_for(waiter, 1) == {}

    run(go())


def test_a_failing_scan_does_not_kill_the_manager():
    async def go():
        backend = FakeBackend()
        calls = {"n": 0}
        original = backend.scan

        async def flaky(duration):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("adapter not ready")
            return await original(duration)

        backend.scan = flaky
        backend.scan_results = [{A: -60}]
        async with Harness(backend) as h:
            await until(lambda: h.manager.connected.is_set())
            assert calls["n"] == 2

    run(go())


def test_session_notifications_reach_the_session():
    async def go():
        backend = FakeBackend()
        backend.scan_results = [{A: -60}]
        async with Harness(backend) as h:
            await until(lambda: h.manager.connected.is_set())
            values = await h.manager.session.read_block(0x3030, 41)
            assert len(values) == 44

    run(go())


def test_link_lost_during_a_read_fails_the_read():
    async def go():
        from solarlife.errors import NotConnectedError

        backend = FakeBackend()
        backend.scan_results = [{A: -60}]
        async with Harness(backend) as h:
            await until(lambda: h.manager.connected.is_set())
            session = h.manager.session
            backend.devices[A].drop_next = 1
            task = asyncio.create_task(session.read_block(0x3030, 41))
            await asyncio.sleep(0.03)
            backend.links[0].trigger_disconnect()
            with pytest.raises(NotConnectedError):
                await task

    run(go())
