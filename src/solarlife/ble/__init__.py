"""Transport interface between the daemon and the BLE stack.

Only ``solarlife.ble.bleak_backend`` imports bleak. Everything else talks to
these protocols, so the daemon can be tested against a fake backend.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from solarlife.errors import ConnectError

MANUFACTURER_ID = 0x03A0
NOTIFY_UUID = "0000ff01-0000-1000-8000-00805f9b34fb"
WRITE_UUID = "0000ff02-0000-1000-8000-00805f9b34fb"

__all__ = [
    "MANUFACTURER_ID",
    "NOTIFY_UUID",
    "WRITE_UUID",
    "Advertisement",
    "BleBackend",
    "BleLink",
    "ConnectError",
]


@dataclass(frozen=True)
class Advertisement:
    """One device seen during a scan. ``rssi`` is the mean over ``samples``."""

    address: str
    name: str | None
    rssi: float
    samples: int
    seen_at: float  # time.monotonic() of the last advertisement
    handle: object | None = None  # backend specific, e.g. a bleak BLEDevice


class BleLink(Protocol):
    """An open connection to one device with notifications on the notify characteristic."""

    address: str

    @property
    def is_connected(self) -> bool: ...

    async def start_notify(self, callback: Callable[[bytes], None]) -> None:
        """Deliver each notification to ``callback`` on the event loop thread."""

    async def write(self, data: bytes) -> None:
        """Write to the write characteristic.

        Raises NotConnectedError if the link is gone, LinkError for other failures.
        """

    async def disconnect(self) -> None: ...


class BleBackend(Protocol):
    async def scan(self, duration_s: float) -> dict[str, Advertisement]:
        """Scan for SolarLife devices (manufacturer ID 0x03A0), keyed by address."""

    async def connect(
        self,
        adv: Advertisement,
        *,
        timeout_s: float,
        on_disconnect: Callable[[], None],
    ) -> BleLink:
        """Open a connection. Raises ConnectError. Must not leave a half-open connect behind."""
