"""CSV output: one file per block and calendar day, with the same columns as MQTT.

* ``<path>/<host>/solarlife/<block>/<host>_<YYYY-MM-DD>_solarlife-<block>.csv``
* ``<path>/<host>/solarlife/events/<host>_<YYYY-MM-DD>_solarlife-events.csv``
  (only the events in ``LOGGED_EVENTS``)

Files are only written with a synced clock, because a wrong clock would put rows
into the wrong day's file (or a file dated 1970). Until then rows go to a spool
per boot and are re-timed by their CLOCK_BOOTTIME when the clock is synced. A
spool left from an earlier boot cannot be re-timed; it becomes
``<host>_unsynced_<boot_id>_solarlife-<key>.csv`` instead. Nothing is ever deleted.
"""

from __future__ import annotations

import json
import logging
import socket
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from solarlife.clock import Clock
from solarlife.outputs.formats import LEADING_COLUMNS, Record, csv_line, event_record, reading_record
from solarlife.reading import Event, Reading

log = logging.getLogger(__name__)

APP = "solarlife"
# Events that are useful for analysing the connection afterwards.
LOGGED_EVENTS = {"connected", "disconnected", "device_switched"}


class CsvOutput:
    def __init__(self, path: Path, hostname: str | None = None, clock: Clock | None = None) -> None:
        self.host = hostname if hostname is not None else socket.gethostname()
        self.dir = Path(path) / self.host / APP
        self.spool_dir = self.dir / ".spool"
        self.clock = clock or Clock(self.spool_dir)
        self.boot_id = self.clock.boot_id()
        self.spool = self.spool_dir / f"{self.boot_id}.jsonl"
        # fails at startup, not with the first reading, when the directory can't be made
        self.dir.mkdir(parents=True, exist_ok=True)
        try:
            self.recover()
        except OSError as exc:
            log.error("Cannot recover the spools in %s: %s", self.spool_dir, exc)

    def daily_file(self, key: str, when: datetime) -> Path:
        return self.dir / key / f"{self.host}_{when:%Y-%m-%d}_{APP}-{key}.csv"

    def unsynced_file(self, key: str, boot_id: str) -> Path:
        return self.dir / key / f"{self.host}_unsynced_{boot_id}_{APP}-{key}.csv"

    @staticmethod
    def _append(file: Path, header: list[str], row: list[Any]) -> None:
        file.parent.mkdir(parents=True, exist_ok=True)
        with file.open("a", encoding="utf-8", newline="") as handle:
            if handle.tell() == 0:
                handle.write(csv_line(header) + "\n")
            handle.write(csv_line(row) + "\n")

    def _write(self, record: Record) -> None:
        if not record.synced:
            self._spool(record)
            return
        if self.spool.exists():
            self.flush_spool()
        self._append(self.daily_file(record.key, record.time), record.header, record.row)

    def emit_reading(self, reading: Reading) -> None:
        self._write(reading_record(reading, self.clock.synced()))

    def emit_event(self, event: Event) -> None:
        if event.kind in LOGGED_EVENTS:
            self._write(event_record(event, self.clock.synced()))

    def emit_state(self, state: dict[str, Any]) -> None:
        return

    def close(self) -> None:
        return

    # --- the spool

    def _spool(self, record: Record) -> None:
        entry = {
            "key": record.key,
            "columns": record.columns,
            "device": record.device,
            "values": record.values,
            "time": record.time.isoformat(),
            "boottime": record.boottime,
        }
        self.spool_dir.mkdir(parents=True, exist_ok=True)
        with self.spool.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, default=str) + "\n")

    @staticmethod
    def _read_spool(spool: Path) -> Iterator[dict[str, Any]]:
        with spool.open(encoding="utf-8") as handle:
            for number, line in enumerate(handle, 1):
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    # e.g. the last line of a spool cut off by a power loss
                    log.warning("Skipping unreadable line %d of %s", number, spool)

    def flush_spool(self) -> None:
        """Re-time this boot's spooled rows with the now synced clock and write them out."""
        offset = self.clock.time() - self.clock.boottime()
        count = 0
        for entry in self._read_spool(self.spool):
            when = datetime.fromtimestamp(round(offset + entry["boottime"])).astimezone()
            header = [*LEADING_COLUMNS, *entry["columns"]]
            self._append(self.daily_file(entry["key"], when), header, [when, False, entry["device"], *entry["values"]])
            count += 1
        self.spool.unlink()
        log.info("Clock synced, wrote %d spooled rows to the daily files", count)

    def recover(self) -> None:
        """Turn spools of earlier boots into `_unsynced_` files; their times cannot be corrected."""
        if not self.spool_dir.is_dir():
            return
        for spool in sorted(self.spool_dir.glob("*.jsonl")):
            if spool == self.spool:
                continue
            boot_id = spool.stem
            count = 0
            for entry in self._read_spool(spool):
                header = [*LEADING_COLUMNS, *entry["columns"]]
                row = [entry["time"], False, entry["device"], *entry["values"]]
                self._append(self.unsynced_file(entry["key"], boot_id), header, row)
                count += 1
            spool.unlink()
            log.warning("Boot %s never had a synced clock, wrote %d rows to its unsynced files", boot_id, count)
