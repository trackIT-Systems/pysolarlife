import csv
import json
from datetime import datetime, timezone

import cbor2

from solarlife.config import MqttConfig
from solarlife.outputs import Outputs
from solarlife.outputs.csv import CsvOutput, csv_session_path
from solarlife.outputs.mqtt import MqttOutput
from solarlife.reading import Event, Reading, Value

WHEN = datetime(2026, 10, 13, 10, 3, 0, 123000, tzinfo=timezone.utc)
MAC = "A4:C1:38:6F:0E:2B"


def reading():
    values = (
        Value("battery_voltage_level", "Battery voltage level", "V", 0x3032, 24.0),
        Value("battery_type", "Battery type", "", 0x9021, "Lithium"),
    )
    return Reading(MAC, "status", WHEN, values)


def test_csv_session_path():
    path = csv_session_path("/data", "tracker", WHEN)
    assert str(path) == "/data/tracker/solarlife/tracker_2026-10-13T100300-solarlife.csv"


def test_csv_rows_and_events(tmp_path):
    out = CsvOutput(tmp_path / "x" / "s.csv")
    out.emit_reading(reading())
    out.emit_event(Event(WHEN, "connected", MAC, {"rssi": -67, "attempts": 5}))
    out.emit_event(Event(WHEN, "connect_failed", MAC, {"error": "timeout"}))  # not logged
    out.emit_state({"state": "connected"})
    rows = list(csv.DictReader((tmp_path / "x" / "s.csv").open()))
    assert rows[0] == {"ts": "2026-10-13T10:03:00.123Z", "device": MAC, "block": "status", "key": "battery_voltage_level", "value": "24.0", "unit": "V"}
    assert rows[1]["key"] == "battery_type" and rows[1]["value"] == "Lithium"
    assert rows[2] == {"ts": "2026-10-13T10:03:00.123Z", "device": MAC, "block": "event", "key": "connected", "value": "rssi=-67 attempts=5", "unit": ""}
    assert len(rows) == 3


def test_csv_reuses_existing_file_without_a_second_header(tmp_path):
    path = tmp_path / "s.csv"
    CsvOutput(path).emit_reading(reading())
    CsvOutput(path).emit_reading(reading())
    assert path.read_text().count("ts,device") == 1


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


def test_mqtt_publishes_json_and_cbor():
    client = FakeMqtt()
    out = MqttOutput(MqttConfig(), hostname="tracker", client=client)
    assert client.connected_to == ("localhost", 1883) and client.looping
    out.emit_reading(reading())
    topics = {t: (p, r) for t, p, r in client.published}
    assert set(topics) == {"tracker/solarlife/a4c1386f0e2b/json", "tracker/solarlife/a4c1386f0e2b/cbor"}
    as_json = json.loads(topics["tracker/solarlife/a4c1386f0e2b/json"][0])
    as_cbor = cbor2.loads(topics["tracker/solarlife/a4c1386f0e2b/cbor"][0])
    assert as_json == as_cbor
    assert as_json["Battery voltage level (V)"] == 24.0 and as_json["Battery type"] == "Lithium"


def test_mqtt_state_is_retained_events_are_not_and_will_marks_offline():
    client = FakeMqtt()
    out = MqttOutput(MqttConfig(), hostname="tracker", client=client)
    assert {(t, json.loads(p)["state"] if t.endswith("json") else cbor2.loads(p)["state"], r) for t, p, r in client.wills} == {
        ("tracker/solarlife/state/json", "offline", True),
        ("tracker/solarlife/state/cbor", "offline", True),
    }
    out.emit_state({"state": "connected"})
    out.emit_event(Event(WHEN, "connected", MAC))
    retained = {t: r for t, _, r in client.published}
    assert retained["tracker/solarlife/state/json"] is True
    assert retained["tracker/solarlife/events/json"] is False


def test_mqtt_format_switches():
    client = FakeMqtt()
    out = MqttOutput(MqttConfig(cbor=False), hostname="t", client=client)
    out.emit_event(Event(WHEN, "connected"))
    assert [t for t, *_ in client.published] == ["t/solarlife/events/json"]
    out.close()
    assert client.looping is False


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
