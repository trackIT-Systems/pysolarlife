"""What the daemon knows right now: cached readings and connection statistics."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from solarlife.config import PollingConfig
from solarlife.reading import Reading, utc_iso


@dataclass
class BlockState:
    reading: Reading | None = None
    ok_at: float | None = None  # monotonic
    last_error: str | None = None
    consecutive_failures: int = 0


class Cache:
    """The last reading per block, with age and staleness."""

    def __init__(self, polling: PollingConfig, clock: Callable[[], float] = time.monotonic) -> None:
        self._intervals = {"status": polling.status_interval_s, "parameters": polling.parameters_interval_s}
        self._factor = polling.stale_after_factor
        self._clock = clock
        self.blocks: dict[str, BlockState] = {name: BlockState() for name in self._intervals}

    def update(self, reading: Reading) -> None:
        state = self.blocks[reading.block]
        state.reading = reading
        state.ok_at = self._clock()
        state.last_error = None
        state.consecutive_failures = 0

    def fail(self, block: str, error: str) -> int:
        """Record a failed read; return the number of consecutive failures of this block."""
        state = self.blocks[block]
        state.last_error = error
        state.consecutive_failures += 1
        return state.consecutive_failures

    def clear(self) -> None:
        """Forget everything, e.g. after switching to another device."""
        self.blocks = {name: BlockState() for name in self._intervals}

    def failures(self) -> int:
        return max((b.consecutive_failures for b in self.blocks.values()), default=0)

    def age_s(self, block: str) -> float | None:
        ok_at = self.blocks[block].ok_at
        return None if ok_at is None else self._clock() - ok_at

    def is_stale(self, block: str) -> bool:
        age = self.age_s(block)
        return age is None or age > self._factor * self._intervals[block]

    def describe(self, block: str) -> dict[str, Any]:
        state = self.blocks[block]
        age = self.age_s(block)
        return {
            "interval_s": self._intervals[block],
            "last_ok": utc_iso(state.reading.time) if state.reading else None,
            "age_s": None if age is None else round(age, 1),
            "stale": self.is_stale(block),
            "last_error": state.last_error,
            "consecutive_failures": state.consecutive_failures,
        }


@dataclass
class Stats:
    """Counters for ``solarlife status``."""

    started: datetime
    connect_attempts: int = 0
    connects: int = 0
    disconnects: int = 0
    last_error: str | None = None
    backoff_s: float | None = None
    last_scan: datetime | None = None
    extra: dict[str, Any] = field(default_factory=dict)
