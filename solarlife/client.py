import asyncio
import logging
from collections.abc import Callable

logger = logging.getLogger(__name__)
from bleak import BleakClient, BleakScanner
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from .protocol import MpptProtocol, ResultContainer, Result


def _results_to_dict(results: ResultContainer) -> dict:
    return {
        (f"{r.friendly_name} ({r.unit})" if r.unit else r.friendly_name): r.value
        for r in results
    }


async def _read_from_address(address: str, timeout: float) -> dict:
    async with MpptClient(address, timeout=timeout) as charger:
        status = await charger.read_status()
        parameters = await charger.read_parameters()
    return _results_to_dict(status + parameters)


def read_device(
    address: str | None = None,
    scan_timeout: float = 10.0,
    timeout: float = 30.0,
) -> dict:
    """Connect to a SolarLife device and return all readings as a dict.

    If *address* is omitted, scans for devices and connects to the one with
    the highest RSSI (strongest signal).  Keys follow the pattern
    ``"Friendly Name (unit)"`` matching the ``--json`` CLI output.

    Args:
        address: BLE MAC address.  If None, auto-selects via scan.
        scan_timeout: Seconds to scan when auto-selecting a device.
        timeout: BLE connection / read timeout in seconds.

    Returns:
        Dict mapping variable names (with units) to their values.

    Raises:
        RuntimeError: If no devices are found during a scan.
    """
    async def _run() -> dict:
        if address is not None:
            return await _read_from_address(address, timeout)

        devices = await MpptClient.scan(timeout=scan_timeout, filter_devices=True)
        if not devices:
            raise RuntimeError("No SolarLife devices found during scan.")

        device, _ = max(
            devices,
            key=lambda d: d[1].rssi if d[1].rssi is not None else float("-inf"),
        )
        logger.info(f"Connecting to device with highest RSSI: {device.address}")
        return await _read_from_address(device.address, timeout)

    return asyncio.run(_run())


class MpptClient(MpptProtocol):
    DEVICE_NAME_UUID = "00002a00-0000-1000-8000-00805f9b34fb"
    NOTIFY_UUID = "0000ff01-0000-1000-8000-00805f9b34fb"
    WRITE_UUID = "0000ff02-0000-1000-8000-00805f9b34fb"
    MANUFACTURER_ID = 0x03A0  # SolarLife Manufacturer ID (Company ID)

    buffer = bytearray()

    def __init__(self, mac_address: str, timeout: float = 30.0):
        self.client = BleakClient(mac_address, timeout=timeout)
        self.response_queue = asyncio.Queue()
        self.lock = asyncio.Lock()
        super().__init__()

    async def __aenter__(self):
        try:
            await self.client.connect()
        except TimeoutError:
            raise TimeoutError(f"Connection timeout: Could not connect to device {self.client.address}. "
                             f"Please ensure the device is powered on, in range, and not connected to another application.")
        await self.client.start_notify(self.NOTIFY_UUID, self.notification_handler)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.client.stop_notify(self.NOTIFY_UUID)
        try:
            await self.client.disconnect()
        except EOFError:
            pass

    def notification_handler(self, characteristic: BleakGATTCharacteristic, data: bytearray):
        if characteristic.uuid != self.NOTIFY_UUID:
            return
        self.buffer += data
        try:
            if not self.is_complete(self.buffer):
                return
            results = self.parse(self.start_address, self.buffer)
            self.response_queue.put_nowait(results)
        except Exception as e:
            logger.debug(f"Response from device: 0x{self.buffer.hex()}")
            logger.error(f"Error while parsing response: {e}")

    async def read(self, start_address: int, count: int, repeat: int = 10, timeout: float = 2) -> ResultContainer:
        async with self.lock:
            self.start_address = start_address
            command = self.get_read_command(0xFE, start_address, count)
            self.response_queue = asyncio.Queue()
            i = 0
            partial_results = ResultContainer([])
            while i < repeat:
                i += 1
                self.buffer = bytearray()
                await self.client.write_gatt_char(self.WRITE_UUID, command)
                try:
                    return await asyncio.wait_for(self.response_queue.get(), timeout=timeout)
                except asyncio.TimeoutError:
                    if self.buffer:
                        logger.debug(f"Got partial response: 0x{self.buffer.hex()}")
                        # Try to parse partial data if we have enough
                        try:
                            if self.is_complete(self.buffer):
                                partial_results = self.parse(self.start_address, self.buffer)
                                return partial_results
                            # If buffer is not complete but has some data, try to parse what we can
                            elif len(self.buffer) >= 4:
                                # Try to parse partial data - this might fail but worth trying
                                try:
                                    partial_results = self.parse(self.start_address, self.buffer)
                                    if partial_results:
                                        logger.debug(f"Successfully parsed partial data ({len(partial_results)} results)")
                                        return partial_results
                                except Exception:
                                    pass  # Partial data not parseable, continue retrying
                        except Exception as e:
                            logger.debug(f"Could not parse partial response: {e}")
                    if i < repeat:
                        logger.debug(f"Repeating read command... (attempt {i+1}/{repeat})")
            # Return any partial results we managed to parse, even if incomplete
            if partial_results:
                logger.debug(f"Returning partial data: {len(partial_results)} results")
                return partial_results
            return ResultContainer([])

    async def read_status(self) -> ResultContainer:
        """Read real-time status registers (address 0x3030, 41 registers)."""
        return await self.read(0x3030, 41)

    async def read_parameters(self) -> ResultContainer:
        """Read battery and load parameter registers (address 0x9021, 12 registers)."""
        return await self.read(0x9021, 12)

    async def write(self, results: list[Result], repeat: int = 10, timeout: float = 2) -> ResultContainer:
        async with self.lock:
            start_address, command = self.get_write_command(self.device_id, results)
            self.start_address = start_address
            self.response_queue = asyncio.Queue()
            i = 0
            while i < repeat:
                i += 1
                self.buffer = bytearray()
                await self.client.write_gatt_char(self.WRITE_UUID, command)
                try:
                    await asyncio.wait_for(self.response_queue.get(), timeout=timeout)
                    return ResultContainer(results)
                except asyncio.TimeoutError:
                    if self.buffer:
                        logger.debug(f"Got partial response: 0x{self.buffer.hex()}")
                    logger.debug(f"Repeating write command...")
            return ResultContainer([])

    async def get_device_name(self) -> str:
        device_name = await self.client.read_gatt_char(self.DEVICE_NAME_UUID)
        return "".join(map(chr, device_name))

    async def list_services(self):
        service_text = "[Service]          "
        charact_text = "  [Characteristic] "
        descrip_text = "    [Descriptor]   "
        value_text   = "      Value = "
        for service in self.client.services:
            logger.info(f"{service_text}{service}")
            for char in service.characteristics:
                logger.info(f"{charact_text}{char} {','.join(char.properties)}")
                for descriptor in char.descriptors:
                    try:
                        logger.info(f"{descrip_text}{descriptor}")
                        value = await self.client.read_gatt_descriptor(descriptor.handle)
                        logger.info(f"{value_text}0x{value.hex()}")
                    except Exception as e:
                        pass

    @staticmethod
    async def scan(
        timeout: float = 10.0,
        filter_devices: bool = True,
        on_found: Callable[[BLEDevice, AdvertisementData], None] | None = None,
    ) -> list[tuple[BLEDevice, AdvertisementData]]:
        """
        Scan for SolarLife MPPT devices by Manufacturer ID 0x03A0.
        
        Args:
            timeout: Scan timeout in seconds (default: 10.0)
            filter_devices: If True, only return SolarLife devices (filtered by Manufacturer ID 0x03A0).
                           If False, return all discovered devices.
            on_found: Optional callback called immediately when a new device is first discovered.
                     Called with (device, advertisement_data) tuple.
        
        Returns:
            List of (BLEDevice, AdvertisementData) tuples matching the filter criteria.
        """
        discovered_devices: dict[str, tuple[BLEDevice, AdvertisementData]] = {}
        
        def detection_callback(device: BLEDevice, advertisement_data: AdvertisementData):
            """Callback function called for each advertisement received."""
            try:
                is_solarlife = False
                if filter_devices:
                    # Check for Manufacturer ID 0x03A0 in advertisement data
                    if advertisement_data.manufacturer_data:
                        if MpptClient.MANUFACTURER_ID in advertisement_data.manufacturer_data:
                            is_solarlife = True
                else:
                    is_solarlife = True
                
                if is_solarlife:
                    # Store device (using address as key to handle duplicates)
                    # Only call on_found for newly discovered devices
                    if device.address not in discovered_devices:
                        discovered_devices[device.address] = (device, advertisement_data)
                        if on_found:
                            on_found(device, advertisement_data)
            except Exception as e:
                # Log but continue scanning even if callback fails
                logger.debug(f"Error in detection callback for {device.address}: {e}")
        
        scanner = None
        try:
            scanner = BleakScanner(detection_callback=detection_callback)
            await scanner.start()
            await asyncio.sleep(timeout)
        except Exception as e:
            logger.error(f"Error during BLE scan: {e}")
            raise
        finally:
            if scanner:
                try:
                    await scanner.stop()
                except Exception as e:
                    logger.debug(f"Error stopping scanner: {e}")
        
        return list(discovered_devices.values())
