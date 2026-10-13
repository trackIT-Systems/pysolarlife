import json
from datetime import datetime, timedelta, timezone

import cbor2

from fakes import FakeDevice
from solarlife.config import PollingConfig
from solarlife.protocol import MpptProtocol
from solarlife.reading import Event, Reading, field_key, mac12, utc_iso
from solarlife.registers import PARAMETERS, STATUS, block_for
from solarlife.state import Cache
from solarlife.variables import variables

WHEN = datetime(2026, 10, 13, 10, 3, 0, 123000, tzinfo=timezone.utc)


def parse_block(block):
    """Run the real protocol parser over the fake device's answer for a block."""
    protocol = MpptProtocol()
    device = FakeDevice()
    cmd = protocol.get_read_command(0xFE, block.address, block.count)
    (frame,) = device.respond(cmd)
    return protocol.parse(block.address, frame)


def test_helpers():
    assert utc_iso(WHEN) == "2026-10-13T10:03:00.123Z"
    assert mac12("AA:BB:CC:DD:EE:FF") == "aabbccddeeff"
    assert field_key("Battery voltage", "V") == "Battery voltage (V)"
    assert field_key("Battery type", "") == "Battery type"


def test_payload_keys_are_unique_within_and_across_blocks():
    # A collision would silently drop a value in the flat MQTT payload.
    keys = []
    for block in (STATUS, PARAMETERS):
        reading = Reading.from_results("AA:BB:CC:DD:EE:FF", block.name, WHEN, parse_block(block))
        assert len(reading.payload()) == len(reading.values) + 1  # + _time
        keys += [v.key for v in reading.values]
    assert len(keys) == len(set(keys))


def test_payload_and_encodings():
    reading = Reading.from_results("AA:BB:CC:DD:EE:FF", "status", WHEN, parse_block(STATUS))
    payload = reading.payload()
    assert "_time" in payload
    assert payload["Current battery voltage level (V)"] == 24.0
    assert json.loads(json.dumps(payload)) == payload
    assert cbor2.loads(cbor2.dumps(payload)) == payload


def test_event_payload_and_detail():
    event = Event(WHEN, "connected", "AA:BB:CC:DD:EE:FF", {"rssi": -67, "attempts": 5})
    assert event.payload()["event"] == "connected"
    assert event.payload()["rssi"] == -67
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
