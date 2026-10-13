import pytest

from solarlife.errors import InvalidWriteError
from solarlife.protocol import MpptProtocol
from solarlife.registers import (
    BLOCKS,
    PARAMETERS,
    PROTECTED,
    STATUS,
    WRITABLE,
    allowed_values,
    block_for,
    blocks_touched,
    build_write,
    plan_writes,
    variables_table,
)
from solarlife.variables import variables


def test_blocks_decode_the_expected_variables():
    for block in BLOCKS.values():
        items = [v for v in variables if block.contains(v.address) and block.function_code in v.function_codes]
        assert items, block.name
    assert len([v for v in variables if block_for(v) is STATUS]) == 44
    assert len([v for v in variables if block_for(v) is PARAMETERS]) == 12


def test_status_block_names_are_unique():
    names = [v.name for v in variables if block_for(v) is STATUS]
    assert len(names) == len(set(names))


def test_writable_lookup_prefers_the_writable_variable():
    assert WRITABLE["battery_type"].address == 0x9021
    assert 0x06 in WRITABLE["battery_type"].function_codes


def test_read_only_and_unknown_names_are_rejected():
    with pytest.raises(InvalidWriteError, match="read-only"):
        build_write({"equipment_id": 1})
    with pytest.raises(InvalidWriteError, match="unknown variable"):
        build_write({"does_not_exist": 1})
    with pytest.raises(InvalidWriteError, match="nothing to write"):
        build_write({})


def test_numbers_are_converted_and_checked():
    (result,) = build_write({"float_voltage": "13.6"})
    assert result.value == 13.6
    with pytest.raises(InvalidWriteError, match="not a number"):
        build_write({"float_voltage": "high"})
    with pytest.raises(InvalidWriteError):
        build_write({"float_voltage": 100000})  # does not fit 16 bit


def test_enums_and_switches_accept_known_values_case_insensitively():
    assert "Lithium" in allowed_values("battery_type")
    (result,) = build_write({"battery_type": "lithium"})
    assert result.value == "Lithium"
    with pytest.raises(InvalidWriteError, match="is not one of"):
        build_write({"battery_type": "plutonium"})
    (result,) = build_write({"manual_control_switch": "on"})
    assert result.value == "On"


def test_protected_variables_need_force():
    assert {"clear_all", "restore_system_default_values", "slave_id"} <= PROTECTED
    with pytest.raises(InvalidWriteError, match="needs force"):
        build_write({"clear_all": "Clear"})
    (result,) = build_write({"clear_all": "Clear"}, force=True)
    assert result.address == 0x000C


def test_built_writes_encode_with_the_protocol():
    results = build_write({"float_voltage": 13.8, "boost_voltage": 14.4})
    for group in plan_writes(results):
        MpptProtocol().get_write_command(0x01, group)


def test_plan_writes_groups_contiguous_registers():
    results = build_write({"boost_voltage": 14.4, "equalizing_voltage": 14.7, "float_voltage": 13.8, "low_voltage_protection_voltage": 10.6})
    groups = plan_writes(results)
    assert [[r.name for r in g] for g in groups] == [
        ["low_voltage_protection_voltage"],
        ["boost_voltage", "equalizing_voltage", "float_voltage"],
    ]


def test_plan_writes_splits_non_contiguous_and_switches():
    results = build_write({"float_voltage": 13.8, "boost_voltage": 14.4, "manual_control_switch": "On", "test_key_trigger": "Off"})
    groups = plan_writes(results)
    assert [[r.name for r in g] for g in groups] == [
        ["manual_control_switch"],
        ["test_key_trigger"],
        ["boost_voltage"],
        ["float_voltage"],
    ]


def test_blocks_touched():
    assert blocks_touched(build_write({"float_voltage": 13.8})) == [PARAMETERS]
    assert blocks_touched(build_write({"manual_control_switch": "On"})) == []


def test_variables_table():
    rows = variables_table()
    assert len(rows) == len(variables)
    by_name = {r["name"]: r for r in rows if r["access"] == "rw"}
    assert by_name["battery_type"]["block"] == "parameters"
    assert "Lithium" in by_name["battery_type"]["allowed"]
    assert by_name["float_voltage"]["allowed"] == []
