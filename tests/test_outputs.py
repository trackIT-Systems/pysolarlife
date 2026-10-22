import json
from datetime import datetime, timedelta, timezone

import cbor2

from fakes import FakeClock, FakeMqtt, FakeScheduler
from solarlife.columns import COLUMNS
from solarlife.config import MqttConfig
from solarlife.outputs import Outputs
from solarlife.outputs.csv import CsvOutput
from solarlife.outputs.formats import encode_cbor, encode_csv, encode_json, reading_record
from solarlife.outputs.mqtt import MqttOutput
from solarlife.reading import Event, Reading, Value

TZ = timezone(timedelta(hours=2))
WHEN = datetime(2026, 10, 13, 12, 3, 0, 123000, tzinfo=TZ)
# How the outputs write WHEN: in the local time zone (the runner's), whole seconds
LOCAL = WHEN.astimezone().replace(microsecond=0).isoformat()
MAC = "A4:C1:38:6F:0E:2B"


def reading(voltage=24.0, when=WHEN, block="status"):
    if block == "status":
        values = (Value("battery_voltage_level", "Battery voltage level", "V", 0x3032, voltage),)
    else:
        values = (Value("battery_type", "Battery type", "", 0x9021, "Lithium"),)
    return Reading(MAC, block, when, values, boottime=50.0)


def mqtt(**config):
    client, scheduler, now = FakeMqtt(), FakeScheduler(), [1000.0]
    out = MqttOutput(
        MqttConfig(**config), hostname="tracker", client=client, clock=FakeClock(synced=True),
        monotonic=lambda: now[0], scheduler=scheduler,
    )
    return out, client, scheduler, now


def topics(client):
    return [t for t, _, _ in client.published]


# --- encodings


def test_all_formats_carry_the_same_columns_in_the_same_order():
    record = reading_record(reading(), synced=True)
    as_json = json.loads(encode_json(record))
    as_cbor = cbor2.loads(encode_cbor(record))
    as_csv = encode_csv(record).decode().split(";")
    assert list(as_json)[:4] == ["time", "synced", "device", COLUMNS["status"][0]]
    assert len(as_json) == len(as_cbor) == len(as_csv) == 3 + len(COLUMNS["status"])
    assert list(as_json.values())[1:] == as_cbor[1:]
    assert as_json["battery_voltage_level"] == 24.0 and as_json["battery_current"] is None


def test_time_has_its_offset_and_whole_seconds_and_cbor_uses_tag_0():
    record = reading_record(reading(), synced=False)
    assert json.loads(encode_json(record))["time"] == LOCAL
    assert encode_csv(record).startswith(f"{LOCAL};False;A4:C1:38:6F:0E:2B;".encode())
    raw = encode_cbor(record)
    assert raw[:3] == bytes([0x98, 3 + len(COLUMNS["status"]), 0xC0])  # array header (2 bytes), then tag 0
    assert cbor2.loads(raw)[0].utcoffset() == WHEN.astimezone().utcoffset()


# --- MQTT


def test_mqtt_publishes_csv_json_and_cbor_per_block():
    out, client, _, _ = mqtt()
    assert client.connected_to == ("localhost", 1883) and client.looping
    out.emit_reading(reading())
    assert topics(client) == [f"tracker/solarlife/a4c1386f0e2b/status/{fmt}" for fmt in ("csv", "json", "cbor")]
    assert not any(retain for _, _, retain in client.published)


def test_mqtt_holds_back_readings_within_the_interval_and_sends_the_newest():
    out, client, scheduler, now = mqtt(publish_interval_s=300)
    out.emit_reading(reading(1.0))  # first: at once
    now[0] += 60
    out.emit_reading(reading(2.0))
    now[0] += 60
    out.emit_reading(reading(3.0))
    assert len(topics(client)) == 3  # only the first so far
    assert [round(h.delay) for h in scheduler.calls] == [240]  # one flush, at the end of the interval
    now[0] += 180
    scheduler.run_due()
    sent = [json.loads(p)["battery_voltage_level"] for t, p, _ in client.published if t.endswith("/json")]
    assert sent == [1.0, 3.0]
    scheduler.run_due()  # nothing pending: nothing sent again
    assert len(topics(client)) == 6


def test_mqtt_rate_limit_is_per_block_and_after_a_quiet_interval_sends_at_once():
    out, client, scheduler, now = mqtt(publish_interval_s=300)
    out.emit_reading(reading())
    out.emit_reading(reading(block="parameters"))  # another block: not limited by the first
    assert len(topics(client)) == 6
    now[0] += 301
    out.emit_reading(reading())
    assert len(topics(client)) == 9 and not scheduler.calls


def test_mqtt_interval_0_publishes_every_reading():
    out, client, scheduler, _ = mqtt(publish_interval_s=0)
    for _ in range(3):
        out.emit_reading(reading())
    assert len(topics(client)) == 9 and not scheduler.calls


def test_mqtt_close_sends_what_is_held_back():
    out, client, scheduler, now = mqtt(publish_interval_s=300)
    out.emit_reading(reading(1.0))
    now[0] += 10
    out.emit_reading(reading(2.0))
    out.close()
    assert [json.loads(p)["battery_voltage_level"] for t, p, _ in client.published if t.endswith("/json")] == [1.0, 2.0]
    assert scheduler.calls[0].cancelled and client.looping is False


def test_mqtt_events_in_three_formats_state_retained_as_map_and_will_offline():
    out, client, _, _ = mqtt()
    assert {(t, r) for t, _, r in client.wills} == {("tracker/solarlife/state/json", True), ("tracker/solarlife/state/cbor", True)}
    assert all(json.loads(p) == {"state": "offline"} for t, p, _ in client.wills if t.endswith("json"))
    out.emit_state({"state": "connected"})
    out.emit_event(Event(WHEN, "connected", MAC, {"rssi": -67}))
    retained = {t: r for t, _, r in client.published}
    assert retained["tracker/solarlife/state/json"] is True and "tracker/solarlife/state/csv" not in retained
    assert retained["tracker/solarlife/events/cbor"] is False
    event = json.loads({t: p for t, p, _ in client.published}["tracker/solarlife/events/json"])
    assert event == {"time": LOCAL, "synced": True, "device": MAC, "event": "connected", "detail": "rssi=-67"}


def test_mqtt_format_switches():
    out, client, _, _ = mqtt(csv=False, cbor=False)
    out.emit_event(Event(WHEN, "connected"))
    assert topics(client) == ["tracker/solarlife/events/json"]


# --- CSV files


def test_csv_one_file_per_block_and_day_with_one_header(tmp_path):
    out = CsvOutput(tmp_path, "tracker", clock=FakeClock(synced=True))
    out.emit_reading(reading(1.0))
    out.emit_reading(reading(2.0))
    out.emit_reading(reading(3.0, when=WHEN + timedelta(days=1)))
    out.emit_reading(reading(block="parameters"))
    base = tmp_path / "tracker" / "solarlife"
    day1 = (base / "status" / "tracker_2026-10-13_solarlife-status.csv").read_text(encoding="utf-8").splitlines()
    assert day1[0] == ";".join(["time", "synced", "device", *COLUMNS["status"]])
    assert len(day1) == 3 and day1[1].startswith(f"{LOCAL};True;A4:C1:38:6F:0E:2B;")
    assert (base / "status" / "tracker_2026-10-14_solarlife-status.csv").exists()
    assert (base / "parameters" / "tracker_2026-10-13_solarlife-parameters.csv").exists()


def test_csv_logs_only_the_connection_events(tmp_path):
    out = CsvOutput(tmp_path, "tracker", clock=FakeClock(synced=True))
    out.emit_event(Event(WHEN, "connected", MAC, {"rssi": -67, "attempts": 5}))
    out.emit_event(Event(WHEN, "connect_failed", MAC, {"error": "timeout"}))
    out.emit_state({"state": "connected"})
    lines = (tmp_path / "tracker/solarlife/events/tracker_2026-10-13_solarlife-events.csv").read_text(encoding="utf-8").splitlines()
    assert lines == ["time;synced;device;event;detail", f"{LOCAL};True;{MAC};connected;rssi=-67 attempts=5"]


def test_outputs_isolates_failing_sinks():
    class Broken:
        def emit_reading(self, r):
            raise RuntimeError("boom")

        emit_event = emit_state = close = emit_reading

    class Good:
        def __init__(self):
            self.seen = []

        def emit_reading(self, r):
            self.seen.append(r)

        def emit_event(self, e): ...
        def emit_state(self, s): ...
        def close(self): ...

    good = Good()
    outputs = Outputs([Broken(), good])
    outputs.emit_reading(reading())
    outputs.emit_event(Event(WHEN, "x"))
    outputs.close()
    assert len(good.seen) == 1


def test_a_sink_failing_on_every_reading_is_logged_once_and_its_recovery_once(caplog):
    class Flaky:
        broken = True

        def emit_reading(self, r):
            if self.broken:
                raise RuntimeError("boom")

        def emit_event(self, e): ...
        def emit_state(self, s): ...
        def close(self): ...

    sink = Flaky()
    outputs = Outputs([sink])
    with caplog.at_level("DEBUG", logger="solarlife.outputs"):
        for _ in range(3):
            outputs.emit_reading(reading())
        sink.broken = False
        outputs.emit_reading(reading())
        outputs.emit_reading(reading())
    levels = [(r.levelname, r.getMessage()) for r in caplog.records if r.name == "solarlife.outputs"]
    assert levels == [
        ("ERROR", "Output Flaky failed in emit_reading: RuntimeError: boom"),
        ("DEBUG", "Output Flaky failed again in emit_reading: RuntimeError: boom"),
        ("DEBUG", "Output Flaky failed again in emit_reading: RuntimeError: boom"),
        ("INFO", "Output Flaky works again in emit_reading"),
    ]
