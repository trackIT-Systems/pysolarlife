# Solarlife Device Advertisement Investigation

## Summary

Investigation of BLE advertisement data for Solarlife MPPT device `10:23:81:0C:0D:C6` reveals detailed advertisement information that can be used for device discrimination.

## Device Advertisement Data

### Target Device: `10:23:81:0C:0D:C6`

**Basic Information:**
- **Address**: `10:23:81:0C:0D:C6`
- **Name**: `SolarLife`
- **Local Name**: `SolarLife` (advertised in advertisement data)
- **RSSI**: -51 dBm (strong signal)
- **Advertisement packets**: 23 received during scan

### Manufacturer Data

**Company ID**: `0x03A0` (decimal: 928)

**Data**: `1023810c0dc6` (6 bytes)
- Hex: `10 23 81 0C 0D C6`
- **Observation**: The manufacturer data contains the device's MAC address!

This is a unique identifier that can be used for discrimination:
- Company ID `0x03A0` appears to be specific to Solarlife devices
- The 6-byte data field contains the MAC address bytes

### Service UUIDs

**Advertised Service UUIDs**: None

The device does not advertise any service UUIDs in its BLE advertisements. Services are discovered only after connection.

### Service Data

**Service Data**: None

### Other Advertisement Fields

- **TX Power**: Not advertised
- **Advertising Flags**: `0x05`

## Discrimination Methods

### Method 1: Device Name (Primary)
- **Criterion**: `device.name == "SolarLife"` (case-insensitive)
- **Reliability**: High - unique in the scanned environment
- **Pros**: Simple, works with basic scanning
- **Cons**: Name could potentially be changed

### Method 2: Manufacturer Data (Most Reliable)
- **Criterion**: 
  - Company ID: `0x03A0` (928 decimal)
  - Data length: 6 bytes
  - Data pattern: Contains MAC address
- **Reliability**: Very High - manufacturer-specific identifier
- **Pros**: Manufacturer-specific, cannot be easily spoofed
- **Cons**: Requires callback-based scanning to capture

### Method 3: Combined Approach (Recommended)
Use both name and manufacturer data for robust discrimination:
1. Filter by name `"SolarLife"` for quick identification
2. Verify with manufacturer data (Company ID `0x03A0`) for confirmation

## The MAC address as identity on every system

The manufacturer data carries the MAC address, which `solarlifed` uses on macOS: CoreBluetooth hides the MAC address and hands out a UUID that is only valid on that Mac, so the daemon takes the six bytes from the advertisement instead (`solarlife.ble.bleak_backend.device_identity`). Where the Bluetooth stack reports a MAC address (Linux, Windows) it is used as it is. Only the one device `10:23:81:0C:0D:C6` below was examined; that the six bytes are the MAC address on every SolarLife model is an assumption (see [unknowns.md](unknowns.md)).

## Comparison with Other Devices

During scanning, other devices showed:
- **Company ID 0x004C** (76 decimal) - Apple devices (most common)
- **Company ID 0x0075** (117 decimal) - Various devices
- **Company ID 0x03A0** (928 decimal) - **Only found on Solarlife device**

The Solarlife device is the **only device** with:
- Company ID `0x03A0`
- Name `"SolarLife"`
- Manufacturer data containing MAC address

## Implementation Notes

### Using Callback-Based Scanning

To capture manufacturer data, use BleakScanner with a callback:

```python
from bleak import BleakScanner
from bleak.backends.scanner import AdvertisementData

def detection_callback(device, advertisement_data: AdvertisementData):
    if advertisement_data.manufacturer_data:
        for company_id, data in advertisement_data.manufacturer_data.items():
            if company_id == 0x03A0:  # Solarlife Company ID
                print(f"Found Solarlife device: {device.address}")
                print(f"Manufacturer data: {data.hex()}")

scanner = BleakScanner(detection_callback=detection_callback)
await scanner.start()
await asyncio.sleep(10.0)
await scanner.stop()
```

### Simple Name-Based Filtering

For basic use cases, name filtering is sufficient:

```python
devices = await BleakScanner.discover()
solarlife_devices = [d for d in devices if d.name == "SolarLife"]
```

## Company ID Research

Company ID `0x03A0` (928 decimal) appears to be assigned to Solarlife or their manufacturer. This is a Bluetooth SIG-assigned Company Identifier that uniquely identifies the manufacturer.

## Files Created

- `examples/investigate_advertisements.py` - Callback-based scanner for detailed advertisement capture
- `examples/investigate_services.py` - Service discovery script (requires connection)
