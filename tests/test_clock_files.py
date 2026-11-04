"""Readings taken before the clock is trusted only go over MQTT; the files start with the sync."""

import logging
from datetime import datetime

from fakes import FakeClock
from solarlife.columns import COLUMNS
from solarlife.outputs.csv import CsvOutput
from solarlife.reading import Reading, Value

MAC = "A4:C1:38:6F:0E:2B"
# Position of battery_voltage_level in a file row (after time, device)
VOLTAGE = 2 + COLUMNS["status"].index("battery_voltage_level")


def local(*args):
    return datetime(*args).astimezone()


# A wrong clock before the sync (the image's build date, say)
WRONG = local(2020, 1, 1).timestamp()


def reading(clock, voltage=24.0):
    """A reading stamped like the poller stamps it, with the fake clock's time."""
    value = Value("battery_voltage_level", "Battery voltage level", "V", 0x3032, voltage)
    return Reading(MAC, "status", datetime.fromtimestamp(clock.time()).astimezone(), (value,))


def status_dir(tmp_path):
    return tmp_path / "tracker" / "solarlife" / "status"


def lines(path):
    return path.read_text(encoding="utf-8").splitlines()


def test_readings_without_a_trusted_clock_are_not_written(tmp_path):
    clock = FakeClock(WRONG)
    out = CsvOutput(tmp_path, "tracker", clock=clock)
    out.emit_reading(reading(clock))
    assert not status_dir(tmp_path).exists()


def test_readings_are_written_from_the_sync_on(tmp_path):
    clock = FakeClock(WRONG)
    out = CsvOutput(tmp_path, "tracker", clock=clock)
    out.emit_reading(reading(clock, voltage=1.0))
    clock.sync(local(2026, 10, 19, 12, 0, 30).timestamp())
    out.emit_reading(reading(clock, voltage=2.0))

    rows = lines(status_dir(tmp_path) / "tracker_2026-10-19_solarlife-status.csv")
    assert len(rows) == 2
    assert rows[1].startswith(local(2026, 10, 19, 12, 0, 30).isoformat())
    assert rows[1].split(";")[VOLTAGE] == "2.0"


def test_dropping_is_logged_when_it_starts_and_ends(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="solarlife.outputs.csv")
    clock = FakeClock(WRONG)
    out = CsvOutput(tmp_path, "tracker", clock=clock)
    for _ in range(3):
        out.emit_reading(reading(clock))
    clock.sync(local(2026, 10, 19, 12).timestamp())
    for _ in range(3):
        out.emit_reading(reading(clock))
    assert [r.getMessage().split(":")[0] for r in caplog.records] == ["The clock is not trusted yet", "The clock is trusted"]
