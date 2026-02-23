#!/usr/bin/env python3
"""
Read all status registers from a Solarlife MPPT charge controller and print them.

Usage:
    python examples/read_all.py <BLE device address>

To find the address of your device, run:
    python examples/read_all.py --scan
"""

import argparse
import asyncio

from solarlife import MpptClient


async def read_all(address: str) -> None:
    async with MpptClient(address) as charger:
        status = await charger.read_status()
        if not status:
            print("No data received.")
            return
        for result in status:
            unit = f" {result.unit}" if result.unit else ""
            print(f"{result.friendly_name}: {result.value}{unit}")


async def scan() -> None:
    print("Scanning for BLE devices...")
    devices = await MpptClient.scan()
    if not devices:
        print("No devices found.")
    else:
        for device in devices:
            print(f"  {device.address}  {device.name}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Solarlife MPPT BLE example")
    parser.add_argument("address", nargs="?", help="BLE device address")
    parser.add_argument("--scan", action="store_true", help="Scan for nearby BLE devices")
    args = parser.parse_args()

    if args.scan:
        asyncio.run(scan())
    elif args.address:
        asyncio.run(read_all(args.address))
    else:
        parser.print_help()
