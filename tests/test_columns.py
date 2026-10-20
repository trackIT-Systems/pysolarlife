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
    text = DOC.read_text()
    for variables in BLOCK_VARIABLES.values():
        for variable in variables:
            assert f"| `{variable.name}` | {variable.unit} |" in text, variable.name
    for column in EVENT_COLUMNS:
        assert f"`{column}`" in text
