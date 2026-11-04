"""One record (a reading or an event) and its CSV, JSON and CBOR encodings.

Over MQTT, all three carry the same columns in the same order: `device`, then
the block's columns (see columns.py). There is no time: a reading is a snapshot
of now, and the receiver stamps it on arrival (tsOS-base docs/time.md). CBOR is
a list without key names, the smallest over cellular; the column order is what
the JSON keys show. The CSV files lead with `time`.
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import cbor2

from solarlife.columns import COLUMNS, EVENT_COLUMNS, row
from solarlife.reading import Event, Reading

EVENTS = "events"


@dataclass(frozen=True)
class Record:
    # What the record is: a block name, or "events"
    key: str
    columns: tuple[str, ...]
    values: tuple[Any, ...]
    # Timezone-aware, whole seconds; only in the CSV files
    time: datetime
    # Whether `time` can be trusted (chrony has a source); the files only take trusted rows
    synced: bool
    device: str | None

    @property
    def header(self) -> list[str]:
        return ["device", *self.columns]

    @property
    def row(self) -> list[Any]:
        return [self.device, *self.values]

    @property
    def file_header(self) -> list[str]:
        return ["time", *self.header]

    @property
    def file_row(self) -> list[Any]:
        return [self.time, *self.row]


def _second(when: datetime) -> datetime:
    return when.astimezone().replace(microsecond=0)


def reading_record(reading: Reading, synced: bool) -> Record:
    return Record(reading.block, COLUMNS[reading.block], row(reading), _second(reading.time), synced, reading.device)


def event_record(event: Event, synced: bool) -> Record:
    return Record(EVENTS, EVENT_COLUMNS, (event.kind, event.detail()), _second(event.time), synced, event.device)


def _text(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


def csv_line(values: list[Any]) -> str:
    """One `;`-separated line without line ending; None becomes an empty field."""
    buffer = io.StringIO()
    csv.writer(buffer, delimiter=";", lineterminator="\n").writerow([_text(value) for value in values])
    return buffer.getvalue().rstrip("\n")


def encode_csv(record: Record) -> bytes:
    return csv_line(record.row).encode()


def encode_json(record: Record) -> bytes:
    return json.dumps(dict(zip(record.header, [_text(value) for value in record.row], strict=True)), default=str).encode()


def encode_cbor(record: Record) -> bytes:
    # canonical: each float in the smallest form that keeps it exact (24.0 in 3 bytes, not 9)
    return cbor2.dumps(record.row, canonical=True)


ENCODERS = {"csv": encode_csv, "json": encode_json, "cbor": encode_cbor}
