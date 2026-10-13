"""Register blocks that are polled, and validation for writes."""

from __future__ import annotations

import struct
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from solarlife.errors import InvalidWriteError
from solarlife.protocol import MpptProtocol, Result
from solarlife.variables import FunctionCodes, Variable, variables

WRITE_FUNCTION_CODES = {
    FunctionCodes.WRITE_STATUS_REGISTER.value,
    FunctionCodes.WRITE_MEMORY_SINGLE.value,
    FunctionCodes.WRITE_MEMORY_RANGE.value,
}
MAX_ENUM_VALUES = 256


@dataclass(frozen=True)
class Block:
    name: str
    address: int
    count: int
    function_code: int

    def contains(self, address: int) -> bool:
        return self.address <= address < self.address + self.count


STATUS = Block("status", 0x3030, 41, FunctionCodes.READ_MEMORY.value)
PARAMETERS = Block("parameters", 0x9021, 12, FunctionCodes.READ_PARAMETER.value)
BLOCKS: dict[str, Block] = {STATUS.name: STATUS, PARAMETERS.name: PARAMETERS}

# Writable variables by name. Names are unique among writable variables.
WRITABLE: dict[str, Variable] = {v.name: v for v in variables if WRITE_FUNCTION_CODES & set(v.function_codes)}

# Changing these can erase data or lock the device out, so they need an explicit force.
PROTECTED = {
    v.name
    for v in WRITABLE.values()
    if 0x0008 <= v.address <= 0x000C or v.name in {"slave_id", "baud_rate", "device_password"}
}


def block_for(variable: Variable) -> Block | None:
    for block in BLOCKS.values():
        if block.function_code in variable.function_codes and block.contains(variable.address):
            return block
    return None


def blocks_touched(results: Iterable[Result]) -> list[Block]:
    """The polled blocks that overlap with the given (written) results."""
    touched = []
    for block in BLOCKS.values():
        if any(block.contains(r.address) for r in results):
            touched.append(block)
    return touched


@lru_cache(maxsize=None)
def allowed_values(name: str) -> tuple[str, ...] | None:
    """The accepted values of a switch or enum variable, or None for numbers."""
    variable = WRITABLE[name]
    if variable.binary_payload:
        return tuple(variable.binary_payload)
    if variable.func and not variable.multiplier:
        seen: dict[Any, int] = {}
        for raw in range(0x10000):
            try:
                value = variable.func(raw)
            except Exception:
                continue
            if value not in seen:
                seen[value] = raw
                if len(seen) > MAX_ENUM_VALUES:
                    return None
        return tuple(str(v) for v in seen)
    return None


def _coerce(variable: Variable, value: Any) -> Any:
    allowed = allowed_values(variable.name)
    if allowed is not None:
        for candidate in allowed:
            if str(value).strip().lower() == candidate.lower():
                return candidate
        shown = ", ".join(repr(a) for a in allowed)
        raise InvalidWriteError(f"{variable.name}: {value!r} is not one of {shown}")
    if variable.multiplier and not variable.func:
        try:
            return float(value)
        except (TypeError, ValueError):
            raise InvalidWriteError(f"{variable.name}: {value!r} is not a number") from None
    if variable.func:
        return value  # tried in several forms by _check_encodable
    try:
        return int(value)
    except (TypeError, ValueError):
        raise InvalidWriteError(f"{variable.name}: {value!r} is not an integer") from None


def _check_encodable(variable: Variable, value: Any) -> Any:
    """Make sure the protocol can encode the value; return the form that works."""
    candidates = [value]
    if variable.func and not variable.multiplier:
        for convert in (int, float):
            try:
                candidates.append(convert(value))
            except (TypeError, ValueError):
                pass
    protocol = MpptProtocol()
    last: Exception | None = None
    for candidate in candidates:
        try:
            protocol.value_to_bytes(variable, bytearray(4), 0, candidate)
            return candidate
        except (Exception, struct.error) as exc:
            last = exc
    raise InvalidWriteError(f"{variable.name}: cannot encode {value!r}: {last}")


def build_write(values: Mapping[str, Any], *, force: bool = False) -> list[Result]:
    """Validate a write request and turn it into Results, sorted by address."""
    if not values:
        raise InvalidWriteError("nothing to write")
    results: list[Result] = []
    for name, value in values.items():
        variable = WRITABLE.get(name)
        if variable is None:
            known = name in {v.name for v in variables}
            raise InvalidWriteError(f"{name}: {'is read-only' if known else 'unknown variable'}")
        if name in PROTECTED and not force:
            raise InvalidWriteError(f"{name}: protected, needs force")
        coerced = _check_encodable(variable, _coerce(variable, value))
        results.append(Result(**vars(variable), value=coerced))
    results.sort(key=lambda r: r.address)
    return results


def plan_writes(results: list[Result]) -> list[list[Result]]:
    """Group results into writes the device accepts.

    A range write needs contiguous holding registers. Status registers (switches)
    can only be written one at a time.
    """
    groups: list[list[Result]] = []
    for result in sorted(results, key=lambda r: r.address):
        status_only = FunctionCodes.WRITE_STATUS_REGISTER.value in result.function_codes and not (
            {FunctionCodes.WRITE_MEMORY_SINGLE.value, FunctionCodes.WRITE_MEMORY_RANGE.value}
            & set(result.function_codes)
        )
        if groups and not status_only:
            last = groups[-1][-1]
            last_width = 2 if last.is_32_bit else 1
            last_status_only = FunctionCodes.WRITE_STATUS_REGISTER.value in last.function_codes and not (
                {FunctionCodes.WRITE_MEMORY_SINGLE.value, FunctionCodes.WRITE_MEMORY_RANGE.value}
                & set(last.function_codes)
            )
            if not last_status_only and last.address + last_width == result.address:
                groups[-1].append(result)
                continue
        groups.append([result])
    return groups


def variables_table() -> list[dict[str, Any]]:
    """All known variables, for listings."""
    rows = []
    for v in variables:
        block = block_for(v)
        writable = v.name in WRITABLE and WRITABLE[v.name].address == v.address
        rows.append(
            {
                "name": v.name,
                "friendly_name": v.friendly_name,
                "address": f"0x{v.address:04x}",
                "unit": v.unit,
                "access": "rw" if writable else "r",
                "block": block.name if block else None,
                "allowed": list(allowed_values(v.name) or []) if writable else [],
            }
        )
    return rows
