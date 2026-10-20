"""The fixed columns of each block, as CSV, JSON and CBOR carry them.

A block is a fixed register range, so its columns are the readable variables in
that range, in register order. A reading lacking a value has null in its column.
"""

from __future__ import annotations

import logging
from typing import Any

from solarlife.reading import Reading
from solarlife.registers import BLOCKS, Block
from solarlife.variables import Variable, variables

log = logging.getLogger(__name__)

EVENT_COLUMNS = ("event", "detail")


def _block_variables(block: Block) -> tuple[Variable, ...]:
    return tuple(v for v in variables if block.contains(v.address) and block.function_code in v.function_codes)


# block name -> its variables, in column order
BLOCK_VARIABLES: dict[str, tuple[Variable, ...]] = {name: _block_variables(block) for name, block in BLOCKS.items()}

# block name -> column names
COLUMNS: dict[str, tuple[str, ...]] = {name: tuple(v.name for v in vs) for name, vs in BLOCK_VARIABLES.items()}

_dropped: set[tuple[str, str]] = set()


def row(reading: Reading) -> tuple[Any, ...]:
    """The reading's values in its block's column order; None for a value it lacks."""
    columns = COLUMNS[reading.block]
    values = {v.name: v.value for v in reading.values}
    for name in values.keys() - set(columns):
        if (reading.block, name) not in _dropped:
            _dropped.add((reading.block, name))
            log.debug("value %s has no column in block %s, not published", name, reading.block)
    return tuple(values.get(column) for column in columns)
