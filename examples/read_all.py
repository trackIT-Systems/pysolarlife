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
import logging
import sys

from solarlife import MpptClient


async def read_all(address: str) -> None:
    charger = None
    try:
        charger = MpptClient(address)
        async with charger:
            status = await charger.read_status()
            if not status:
                logging.warning("No data received.")
                return
            for result in status:
                unit = f" {result.unit}" if result.unit else ""
                logging.info(f"{result.friendly_name}: {result.value}{unit}")
    except TimeoutError as e:
        logging.warning(f"Timeout occurred: {e}")
        # If we have partial data from a read attempt, try to show it
        if charger and hasattr(charger, 'buffer') and charger.buffer:
            logging.debug(f"Partial data received before timeout: 0x{charger.buffer.hex()}")
            # Try to parse and display partial results
            try:
                if hasattr(charger, 'start_address'):
                    partial_results = charger.parse(charger.start_address, charger.buffer)
                    if partial_results:
                        logging.info(f"Successfully parsed {len(partial_results)} partial results:")
                        for result in partial_results:
                            unit = f" {result.unit}" if result.unit else ""
                            logging.info(f"  {result.friendly_name}: {result.value}{unit}")
            except Exception as parse_error:
                logging.debug(f"Could not parse partial data: {parse_error}")
        else:
            logging.warning("The device may be out of range or not responding.")
            logging.info("Please check:")
            logging.info("  - Device is powered on and in range")
            logging.info("  - BLE address is correct")
            logging.info("  - No other applications are connected to the device")
    except Exception as e:
        logging.error(f"Error occurred: {e}")
        import traceback
        logging.debug(traceback.format_exc())


async def scan(all_devices: bool = False) -> None:
    if all_devices:
        logging.info("Scanning for all BLE devices...")
        devices = await MpptClient.scan(filter_devices=False)
    else:
        logging.info("Scanning for SolarLife MPPT devices (Manufacturer ID 0x03A0)...")
        devices = await MpptClient.scan(filter_devices=True)
    
    if not devices:
        if all_devices:
            logging.warning("No devices found.")
        else:
            logging.warning("No SolarLife devices found.")
            logging.info("Try running with --all to see all BLE devices.")
    else:
        if all_devices:
            logging.info(f"Found {len(devices)} device(s):")
        else:
            logging.info(f"Found {len(devices)} SolarLife device(s):")
        for device in devices:
            name = device.name or "(no name)"
            logging.info(f"  {device.address}  {name}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Solarlife MPPT BLE example")
    parser.add_argument("address", nargs="?", help="BLE device address")
    parser.add_argument("--scan", action="store_true", help="Scan for SolarLife MPPT devices")
    parser.add_argument("--all", action="store_true", help="Show all BLE devices (use with --scan)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    parser.add_argument("-q", "--quiet", action="store_true", help="Only show warnings and errors")
    args = parser.parse_args()

    # Configure logging level based on arguments
    if args.quiet:
        log_level = logging.WARNING
    elif args.verbose:
        log_level = logging.DEBUG
    else:
        log_level = logging.INFO

    # Set up logging with a simple format
    logging.basicConfig(
        level=log_level,
        format="%(message)s",
        stream=sys.stdout
    )

    if args.scan:
        asyncio.run(scan(all_devices=args.all))
    elif args.address:
        asyncio.run(read_all(args.address))
    else:
        parser.print_help()
