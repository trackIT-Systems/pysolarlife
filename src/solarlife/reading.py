"""Readings and events as they are handed to the outputs."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from solarlife.clock import boottime
from solarlife.protocol import Result


def utc_iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def local_iso(when: datetime) -> str:
    return when.astimezone().isoformat(sep=" ", timespec="milliseconds")


def mac12(address: str) -> str:
    """AA:BB:CC:DD:EE:FF -> aabbccddeeff, for topics."""
    return address.replace(":", "").lower()


@dataclass(frozen=True)
class Value:
    name: str
    friendly_name: str
    unit: str
    address: int
    value: Any

    @classmethod
    def from_result(cls, result: Result) -> Value:
        return cls(result.name, result.friendly_name, result.unit, result.address, result.value)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "friendly": self.friendly_name,
            "unit": self.unit,
            "address": f"0x{self.address:04x}",
            "value": self.value,
        }


@dataclass(frozen=True)
class Reading:
    """The values of one register block read from one device."""

    device: str
    block: str
    time: datetime
    values: tuple[Value, ...]
    # CLOCK_BOOTTIME when the reading was taken, to re-time it if the clock was not synced yet
    boottime: float = field(default_factory=boottime)

    @classmethod
    def from_results(cls, device: str, block: str, when: datetime, results) -> Reading:
        return cls(device, block, when, tuple(Value.from_result(r) for r in results))


@dataclass(frozen=True)
class Event:
    time: datetime
    kind: str
    device: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    boottime: float = field(default_factory=boottime)

    def detail(self) -> str:
        """Compact ``k=v`` text for the CSV."""
        return " ".join(f"{k}={v}" for k, v in self.data.items())
