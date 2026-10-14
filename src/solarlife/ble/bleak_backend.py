"""BleBackend on top of bleak (BlueZ on Linux, CoreBluetooth on macOS, WinRT on Windows).

Only API that exists in bleak 0.22 (Debian) and 1.x is used.

* A device is identified by its MAC address on every system. macOS hides it (CoreBluetooth hands
  out a UUID that is only valid on that Mac); the advertisement carries it as manufacturer data,
  which is where it is read from then (see ``device_identity``).

* Connects with the BLEDevice from a scan, never with an address string: bleak would
  otherwise run its own hidden scan first, which BlueZ may refuse while we scan.
* A failed or cancelled connect always ends with a disconnect, so BlueZ is not left
  with a half-open connection that makes the next attempt fail with InProgress.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
import time
from collections.abc import Callable
from typing import Any

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakDBusError, BleakDeviceNotFoundError, BleakError

from solarlife.ble import MANUFACTURER_ID, NOTIFY_UUID, WRITE_UUID, Advertisement
from solarlife.errors import ConnectError, LinkError, NotConnectedError

log = logging.getLogger(__name__)

MAC_ADDRESS = re.compile(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$")
SCANNER_STOP_TIMEOUT_S = 5.0
DISCONNECT_TIMEOUT_S = 10.0
CONNECT_GRACE_S = 5.0


def device_identity(address: str, manufacturer_data: dict[int, bytes] | None) -> str:
    """The MAC address of a device, upper case with colons.

    The stack's address if it is one (Linux, Windows). Otherwise (macOS: a UUID) the six bytes the
    device puts into its manufacturer data, if it does; else the stack's address unchanged.
    """
    if MAC_ADDRESS.match(address):
        return address.upper()
    data = (manufacturer_data or {}).get(MANUFACTURER_ID)
    if data is not None and len(data) == 6:
        return ":".join(f"{byte:02X}" for byte in data)
    return address


def dbus_error_name(exc: BaseException) -> str:
    return getattr(exc, "dbus_error", None) or ""


def map_connect_error(exc: BaseException) -> ConnectError:
    """Translate whatever bleak raised while connecting."""
    if isinstance(exc, BleakDeviceNotFoundError):
        return ConnectError(f"device not found: {exc}", not_found=True)
    if isinstance(exc, BleakDBusError):
        name = dbus_error_name(exc)
        in_progress = name.endswith("InProgress") or "already in progress" in str(exc).lower()
        return ConnectError(f"{name or 'dbus error'}: {exc}", in_progress=in_progress)
    if isinstance(exc, TimeoutError):
        return ConnectError("connect timed out")
    return ConnectError(f"{type(exc).__name__}: {exc}")


def map_write_error(exc: BaseException, connected: bool) -> Exception:
    name = dbus_error_name(exc)
    if not connected or name.endswith("NotConnected") or "not connected" in str(exc).lower():
        return NotConnectedError(str(exc) or "not connected")
    return LinkError(f"{type(exc).__name__}: {exc}")


class BleakLink:
    def __init__(self, address: str, client: Any) -> None:
        self.address = address
        self._client = client

    @property
    def is_connected(self) -> bool:
        return bool(self._client.is_connected)

    async def start_notify(self, callback: Callable[[bytes], None]) -> None:
        try:
            await self._client.start_notify(NOTIFY_UUID, lambda _char, data: callback(bytes(data)))
        except Exception as exc:
            raise LinkError(f"cannot enable notifications: {type(exc).__name__}: {exc}") from exc

    async def write(self, data: bytes) -> None:
        try:
            await self._client.write_gatt_char(WRITE_UUID, data)
        except (BleakError, EOFError, OSError) as exc:
            raise map_write_error(exc, self.is_connected) from exc

    async def disconnect(self) -> None:
        try:
            await self._client.disconnect()
        except EOFError:
            pass  # bleak/dbus-fast raise this if the connection is already gone


class BleakBackend:
    def __init__(self, adapter: str | None = None) -> None:
        self._adapter = adapter
        self._warned_adapter = False

    def _kwargs(self) -> dict[str, Any]:
        if not self._adapter:
            return {}
        if not sys.platform.startswith("linux"):
            if not self._warned_adapter:
                log.warning("ble.adapter is only used with BlueZ (Linux); ignored")
                self._warned_adapter = True
            return {}
        return {"adapter": self._adapter}

    async def scan(self, duration_s: float, *, until_address: str | None = None) -> dict[str, Advertisement]:
        seen: dict[str, dict[str, Any]] = {}
        found = asyncio.Event()

        def on_detect(device: Any, adv: Any) -> None:
            if MANUFACTURER_ID not in (adv.manufacturer_data or {}):
                return
            identity = device_identity(device.address, adv.manufacturer_data)
            entry = seen.setdefault(identity, {"rssi": []})
            if adv.rssi is not None:
                entry["rssi"].append(adv.rssi)
            entry["device"] = device
            entry["name"] = adv.local_name or device.name or entry.get("name")
            entry["seen_at"] = time.monotonic()
            if until_address is not None and identity == until_address:
                found.set()

        scanner = BleakScanner(detection_callback=on_detect, **self._kwargs())
        await scanner.start()
        try:
            try:
                await asyncio.wait_for(found.wait(), duration_s)
            except TimeoutError:
                pass
        finally:
            try:
                await asyncio.wait_for(scanner.stop(), SCANNER_STOP_TIMEOUT_S)
            except Exception as exc:
                log.warning("stopping the scanner failed: %s", exc)

        results = {}
        for address, entry in seen.items():
            rssis = entry["rssi"]
            results[address] = Advertisement(
                address=address,
                name=entry.get("name"),
                rssi=sum(rssis) / len(rssis) if rssis else -127.0,
                samples=len(rssis),
                seen_at=entry["seen_at"],
                handle=entry["device"],
            )
        return results

    async def connect(self, adv: Advertisement, *, timeout_s: float, on_disconnect: Callable[[], None]) -> BleakLink:
        loop = asyncio.get_running_loop()
        client = BleakClient(
            adv.handle if adv.handle is not None else adv.address,
            disconnected_callback=lambda _client: loop.call_soon_threadsafe(on_disconnect),
            timeout=timeout_s,
            **self._kwargs(),
        )
        try:
            async with asyncio.timeout(timeout_s + CONNECT_GRACE_S):
                await client.connect()
        except asyncio.CancelledError:
            await self._cleanup(client)
            raise
        except Exception as exc:
            await self._cleanup(client)
            raise map_connect_error(exc) from exc
        return BleakLink(adv.address, client)

    @staticmethod
    async def _cleanup(client: Any) -> None:
        try:
            await asyncio.wait_for(client.disconnect(), DISCONNECT_TIMEOUT_S)
        except BaseException as exc:  # best effort, also when cancelled again
            log.debug("cleanup disconnect: %r", exc)
