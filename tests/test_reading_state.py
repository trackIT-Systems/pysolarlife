from datetime import datetime, timezone

from solarlife.config import PollingConfig
from solarlife.reading import Event, Reading, mac12, utc_iso
from solarlife.state import Cache

WHEN = datetime(2026, 10, 13, 10, 3, 0, 123000, tzinfo=timezone.utc)


def test_helpers():
    assert utc_iso(WHEN) == "2026-10-13T10:03:00.123Z"
    assert mac12("AA:BB:CC:DD:EE:FF") == "aabbccddeeff"


def test_event_detail():
    event = Event(WHEN, "connected", "AA:BB:CC:DD:EE:FF", {"rssi": -67, "attempts": 5})
    assert event.detail() == "rssi=-67 attempts=5"


def test_cache_age_and_staleness():
    now = [100.0]
    cache = Cache(PollingConfig(status_interval_s=60, parameters_interval_s=3600, stale_after_factor=3), clock=lambda: now[0])
    assert cache.is_stale("status") and cache.age_s("status") is None
    cache.update(Reading.from_results("AA", "status", WHEN, []))
    now[0] += 100
    assert cache.age_s("status") == 100 and not cache.is_stale("status")
    now[0] += 100
    assert cache.is_stale("status")  # 200 s > 3 * 60 s
    assert not cache.is_stale("status") or cache.describe("status")["stale"]


def test_cache_failures_and_clear():
    cache = Cache(PollingConfig())
    assert cache.fail("status", "timeout") == 1
    assert cache.fail("status", "timeout") == 2
    assert cache.failures() == 2
    cache.update(Reading.from_results("AA", "status", WHEN, []))
    assert cache.failures() == 0 and cache.blocks["status"].last_error is None
    cache.clear()
    assert cache.blocks["status"].reading is None
