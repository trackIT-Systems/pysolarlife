from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any, Protocol

from solarlife.reading import Event, Reading

log = logging.getLogger(__name__)


class Output(Protocol):
    def emit_reading(self, reading: Reading) -> None: ...

    def emit_event(self, event: Event) -> None: ...

    def emit_state(self, state: dict[str, Any]) -> None: ...

    def close(self) -> None: ...


class Outputs:
    """Fan out to all sinks. A failing sink never affects the others or the caller."""

    def __init__(self, sinks: Iterable[Output] = ()) -> None:
        self.sinks = list(sinks)
        # The last error logged per sink and method: a sink that fails on every reading is
        # logged when it starts failing or the error changes, the repeats at DEBUG.
        self._errors: dict[str, str] = {}

    def emit_reading(self, reading: Reading) -> None:
        for sink in self.sinks:
            self._call(sink, "emit_reading", reading)

    def emit_event(self, event: Event) -> None:
        for sink in self.sinks:
            self._call(sink, "emit_event", event)

    def emit_state(self, state: dict[str, Any]) -> None:
        for sink in self.sinks:
            self._call(sink, "emit_state", state)

    def close(self) -> None:
        for sink in self.sinks:
            self._call(sink, "close")

    def _call(self, sink: Output, method: str, *args: Any) -> None:
        key = f"{type(sink).__name__}.{method}"
        try:
            getattr(sink, method)(*args)
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            if self._errors.get(key) != message:
                log.exception("Output %s failed in %s: %s", type(sink).__name__, method, message)
            else:
                log.debug("Output %s failed again in %s: %s", type(sink).__name__, method, message)
            self._errors[key] = message
            return
        if self._errors.pop(key, None) is not None:
            log.info("Output %s works again in %s", type(sink).__name__, method)
