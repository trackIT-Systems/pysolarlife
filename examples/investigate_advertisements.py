#!/usr/bin/env python3
"""
Investigate BLE advertisement data for a specific device using callback-based scanning.

This script uses BleakScanner with callbacks to capture detailed advertisement data
including manufacturer_data and service_uuids that may not be available in the
simple discover() method.

Usage:
    python examples/investigate_advertisements.py [device_address]
"""

import argparse
import asyncio
import sys
from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData


def format_bytes(data: bytes) -> str:
    """Format bytes as hex string."""
    return " ".join(f"{b:02X}" for b in data)


# Store discovered devices with their advertisement data
discovered_devices = {}


def detection_callback(device: BLEDevice, advertisement_data: AdvertisementData):
    """Callback function called for each advertisement received."""
    # Store device with its advertisement data
    # Use address as key to handle multiple advertisements from same device
    addr = device.address
    
    # If we've seen this device before, merge/update the data
    if addr not in discovered_devices:
        discovered_devices[addr] = {
            'device': device,
            'advertisement': advertisement_data,
            'count': 1
        }
    else:
        discovered_devices[addr]['count'] += 1
        # Update if we get more complete data
        if advertisement_data.manufacturer_data or advertisement_data.service_uuids:
            discovered_devices[addr]['advertisement'] = advertisement_data


def print_device_advertisement(addr: str, data: dict) -> None:
    """Print detailed advertisement data for a device."""
    device = data['device']
    adv = data['advertisement']
    count = data['count']
    
    print(f"\n{'='*80}")
    print(f"Address: {device.address}")
    print(f"Name: {device.name or '(no name)'}")
    print(f"Advertisement packets received: {count}")
    
    # RSSI from advertisement data
    if adv.rssi is not None:
        print(f"RSSI: {adv.rssi} dBm")
    
    print(f"\nAdvertisement Data:")
    
    # Manufacturer data
    if adv.manufacturer_data:
        print(f"  Manufacturer Data:")
        for company_id, mfg_data in adv.manufacturer_data.items():
            print(f"    Company ID: 0x{company_id:04X} (decimal: {company_id})")
            print(f"    Data length: {len(mfg_data)} bytes")
            print(f"    Data (hex): {mfg_data.hex()}")
            print(f"    Data (bytes): {format_bytes(mfg_data)}")
            # Try to interpret as ASCII if printable
            try:
                ascii_str = ''.join(chr(b) if 32 <= b < 127 else '.' for b in mfg_data)
                if any(c.isprintable() for c in ascii_str):
                    print(f"    Data (ASCII): {ascii_str}")
            except:
                pass
    else:
        print(f"  Manufacturer Data: (none)")
    
    # Service UUIDs
    if adv.service_uuids:
        print(f"  Service UUIDs ({len(adv.service_uuids)}):")
        for uuid in sorted(adv.service_uuids):
            print(f"    - {uuid}")
    else:
        print(f"  Service UUIDs: (none)")
    
    # Service data
    if adv.service_data:
        print(f"  Service Data:")
        for uuid, svc_data in adv.service_data.items():
            print(f"    UUID: {uuid}")
            print(f"    Data length: {len(svc_data)} bytes")
            print(f"    Data (hex): {svc_data.hex()}")
            print(f"    Data (bytes): {format_bytes(svc_data)}")
    else:
        print(f"  Service Data: (none)")
    
    # Local name
    if adv.local_name:
        print(f"  Local Name: {adv.local_name}")
    else:
        print(f"  Local Name: (none)")
    
    # TX Power
    if adv.tx_power is not None:
        print(f"  TX Power: {adv.tx_power} dBm")
    else:
        print(f"  TX Power: (none)")
    
    # Platform-specific data
    if adv.platform_data:
        print(f"  Platform Data: {adv.platform_data}")


async def investigate_device(target_address: str = None, timeout: float = 10.0) -> None:
    """Scan for BLE devices using callback-based scanning to capture detailed advertisement data."""
    print("Scanning for BLE devices with detailed advertisement capture...")
    print(f"(Scanning for {timeout} seconds)")
    print("(This captures manufacturer_data and service_uuids that may not appear in simple scans)")
    
    # Clear previous results
    discovered_devices.clear()
    
    # Create scanner with callback
    scanner = BleakScanner(detection_callback=detection_callback)
    
    # Start scanning
    await scanner.start()
    await asyncio.sleep(timeout)
    await scanner.stop()
    
    if not discovered_devices:
        print("\nNo devices found.")
        return
    
    print(f"\nFound {len(discovered_devices)} unique device(s)")
    
    # Normalize target address if provided
    target_addr_normalized = None
    if target_address:
        target_addr_normalized = target_address.upper().replace(":", "").replace("-", "")
    
    target_data = None
    other_devices = []
    
    # Separate target from others
    for addr, data in discovered_devices.items():
        addr_normalized = addr.upper().replace(":", "").replace("-", "")
        if target_address and addr_normalized == target_addr_normalized:
            target_data = (addr, data)
        else:
            other_devices.append((addr, data))
    
    # Print target device first
    if target_data:
        addr, data = target_data
        print(f"\n{'='*80}")
        print(">>> TARGET DEVICE <<<")
        print_device_advertisement(addr, data)
    elif target_address:
        print(f"\n⚠️  Target device {target_address} not found in scan results.")
        print(f"\nFound devices:")
        for addr, data in discovered_devices.items():
            device = data['device']
            print(f"  - {device.address} ({device.name or 'no name'})")
        return
    
    # Print other devices for comparison
    if other_devices:
        print(f"\n{'='*80}")
        print(f"OTHER DEVICES ({len(other_devices)}):")
        for addr, data in other_devices:
            print_device_advertisement(addr, data)
    
    # Summary comparison
    if target_data and other_devices:
        print(f"\n{'='*80}")
        print("COMPARISON SUMMARY:")
        print(f"{'='*80}")
        
        target_addr, target_data_dict = target_data
        target_adv = target_data_dict['advertisement']
        
        # Compare manufacturer data
        print(f"\nTarget device manufacturer data:")
        if target_adv.manufacturer_data:
            for company_id, data in target_adv.manufacturer_data.items():
                print(f"  Company ID: 0x{company_id:04X}, Data: {data.hex()}")
        else:
            print(f"  (none)")
        
        print(f"\nOther devices with manufacturer data:")
        found_any = False
        for addr, data_dict in other_devices:
            adv = data_dict['advertisement']
            if adv.manufacturer_data:
                found_any = True
                device = data_dict['device']
                for company_id, mfg_data in adv.manufacturer_data.items():
                    print(f"  {device.address}: Company ID: 0x{company_id:04X}, Data: {mfg_data.hex()}")
        if not found_any:
            print(f"  (none)")
        
        # Compare service UUIDs
        print(f"\nTarget device service UUIDs:")
        if target_adv.service_uuids:
            for uuid in sorted(target_adv.service_uuids):
                print(f"  - {uuid}")
        else:
            print(f"  (none)")
        
        print(f"\nOther devices service UUIDs:")
        found_any = False
        for addr, data_dict in other_devices:
            adv = data_dict['advertisement']
            if adv.service_uuids:
                found_any = True
                device = data_dict['device']
                print(f"  {device.address}:")
                for uuid in sorted(adv.service_uuids):
                    print(f"    - {uuid}")
        if not found_any:
            print(f"  (none)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Investigate BLE advertisement data using callback-based scanning",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "address",
        nargs="?",
        help="Specific BLE device address to investigate (e.g., 10:23:81:0C:0D:C6)"
    )
    parser.add_argument(
        "-t", "--timeout",
        type=float,
        default=10.0,
        help="Scan timeout in seconds (default: 10.0)"
    )
    args = parser.parse_args()
    
    asyncio.run(investigate_device(args.address, args.timeout))
