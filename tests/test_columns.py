"""The fixed columns of each block cover every value a block read produces."""

from datetime import datetime, timezone
from pathlib import Path

from fakes import FakeDevice
from solarlife.columns import BLOCK_VARIABLES, COLUMNS, EVENT_COLUMNS, row
from solarlife.protocol import MpptProtocol
from solarlife.reading import Reading
from solarlife.registers import BLOCKS

WHEN = datetime(2026, 10, 13, 10, 3, tzinfo=timezone.utc)
DOC = Path(__file__).resolve().parent.parent / "docs" / "outputs.md"


# The columns of 1.1.0 with their units, per block. From 1.1.0 on, a column is only ever appended: none
# is removed, renamed, moved or changes its unit, so a consumer decodes a CBOR array by position
# (docs/outputs.md). The columns follow register order: a variable decoded later inside a block's range
# must still go to the end of its block's columns.
COLUMNS_1_1 = {
    "status": (
        ("equipment_id", ""),
        ("run_days", ""),
        ("battery_voltage_level", "V"),
        ("battery_temperature_protection_status", ""),
        ("battery_voltage_protection_status", ""),
        ("solar_panel_charge_disabled", ""),
        ("solar_panel_is_night", ""),
        ("solar_panel_charge_over_temperature", ""),
        ("solar_panel_charge_state", ""),
        ("solar_panel_charge_fault", ""),
        ("solar_panel_is_charging", ""),
        ("load_state", ""),
        ("output_short_circuit", ""),
        ("output_hardware_protection", ""),
        ("output_open_circuit_protection", ""),
        ("output_over_temperature", ""),
        ("output_fault", ""),
        ("load_is_enabled", ""),
        ("environment_temperature", "℃"),
        ("device_built_in_temperature", "℃"),
        ("battery_empty_times", ""),
        ("battery_full_times", ""),
        ("over_voltage_protection_times", ""),
        ("over_current_protection_times", ""),
        ("short_circuit_protection_times", ""),
        ("open_circuit_protection_times", ""),
        ("hardware_protection_times", ""),
        ("charge_over_temperature_protection_times", ""),
        ("discharge_over_temperature_protection_times", ""),
        ("battery_percentage", "%"),
        ("battery_voltage", "V"),
        ("battery_current", "A"),
        ("battery_power", "W"),
        ("load_voltage", "V"),
        ("load_current", "A"),
        ("load_power", "W"),
        ("solar_panel_voltage", "V"),
        ("solar_panel_current", "A"),
        ("solar_panel_power", "W"),
        ("solar_panel_daily_energy", "kWh"),
        ("solar_panel_total_energy", "kWh"),
        ("load_daily_energy", "kWh"),
        ("load_total_energy", "kWh"),
        ("total_light_time_during_the_day", "min"),
    ),
    "parameters": (
        ("battery_type", ""),
        ("low_voltage_protection_voltage", "V"),
        ("low_voltage_recovery_voltage", "V"),
        ("boost_voltage", "V"),
        ("equalizing_voltage", "V"),
        ("float_voltage", "V"),
        ("system_rated_voltage_level", ""),
        ("charge_target_voltage_for_lithium", "V"),
        ("charge_recovery_voltage_for_lithium", "V"),
        ("charging_at_zero_celsius", ""),
        ("mt_series_load_mode", ""),
        ("mt_series_manual_control_default", ""),
    ),
}


def parse_block(block):
    """Run the real protocol parser over the fake device's answer for a block."""
    protocol = MpptProtocol()
    device = FakeDevice()
    cmd = protocol.get_read_command(0xFE, block.address, block.count)
    (frame,) = device.respond(cmd)
    return protocol.parse(block.address, frame)


def test_a_block_read_fills_exactly_the_blocks_columns():
    for name, block in BLOCKS.items():
        reading = Reading.from_results("AA:BB:CC:DD:EE:FF", name, WHEN, parse_block(block))
        assert [v.name for v in reading.values] == list(COLUMNS[name])
        assert None not in row(reading)


def test_column_names_are_unique_within_a_block():
    for columns in COLUMNS.values():
        assert len(columns) == len(set(columns))


def test_a_missing_value_is_none_and_an_unknown_one_is_dropped():
    reading = Reading.from_results("AA:BB:CC:DD:EE:FF", "status", WHEN, parse_block(BLOCKS["status"])[:2])
    values = row(reading)
    assert len(values) == len(COLUMNS["status"]) and values[2:] == (None,) * (len(values) - 2)


def test_docs_list_every_column_with_its_unit():
    text = DOC.read_text(encoding="utf-8")
    for variables in BLOCK_VARIABLES.values():
        for variable in variables:
            assert f"| `{variable.name}` | {variable.unit} |" in text, variable.name
    for column in EVENT_COLUMNS:
        assert f"`{column}`" in text


def test_columns_of_1_1_keep_their_position_and_unit():
    for name, pinned in COLUMNS_1_1.items():
        current = tuple((v.name, v.unit) for v in BLOCK_VARIABLES[name])
        assert current[: len(pinned)] == pinned, name
    assert EVENT_COLUMNS == ("event", "detail")
