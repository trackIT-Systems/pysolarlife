#!/usr/bin/env python3
"""
Solarlife CLI tool for scanning and reading SolarLife MPPT charge controllers.
"""

import argparse
import asyncio
import json
import logging
import sys
from typing import Optional

from solarlife import MpptClient
from solarlife.client import _results_to_dict, _read_from_address

logger = logging.getLogger(__name__)


def setup_logging(verbose: bool, quiet: bool) -> None:
    """Configure logging based on verbosity flags."""
    if quiet:
        log_level = logging.WARNING
    elif verbose:
        log_level = logging.DEBUG
    else:
        log_level = logging.INFO

    logging.basicConfig(
        level=log_level,
        format="%(message)s",
        stream=sys.stderr
    )


async def cmd_scan(timeout: float) -> None:
    """Scan for SolarLife devices and print them as they appear."""
    logger.info("Scanning for SolarLife MPPT devices (Manufacturer ID 0x03A0)...")
    
    def on_found(device, advertisement_data):
        """Print device immediately when discovered."""
        name = device.name or "(no name)"
        rssi = advertisement_data.rssi if advertisement_data.rssi is not None else "N/A"
        if rssi != "N/A":
            print(f"{device.address}  {name}  RSSI: {rssi}dBm")
        else:
            print(f"{device.address}  {name}")
    
    devices = await MpptClient.scan(timeout=timeout, filter_devices=True, on_found=on_found)
    
    if not devices:
        logger.warning("No SolarLife devices found.")


async def cmd_read(
    address: Optional[str],
    json_output: bool,
    scan_timeout: float,
    timeout: float,
) -> None:
    """Read all available outputs from a SolarLife device."""
    device_address = address

    # If no address provided, scan for highest RSSI device
    if not device_address:
        logger.info(f"Scanning for SolarLife devices (timeout: {scan_timeout}s)...")
        devices = await MpptClient.scan(timeout=scan_timeout, filter_devices=True)

        if not devices:
            logger.error("No SolarLife devices found during scan.")
            sys.exit(1)

        best_device, best_ad = max(
            devices,
            key=lambda d: d[1].rssi if d[1].rssi is not None else float("-inf"),
        )
        device_address = best_device.address
        logger.info(f"Connecting to device with highest RSSI: {device_address} ({best_ad.rssi}dBm)")

    # Connect and read data
    try:
        all_results = await _read_from_address(device_address, timeout)

        if not all_results:
            logger.warning("No data received.")
            return

        if json_output:
            print(json.dumps(_results_to_dict(all_results), indent=2))
        else:
            for result in all_results:
                unit = f" {result.unit}" if result.unit else ""
                print(f"{result.friendly_name}: {result.value}{unit}")
                    
    except TimeoutError as e:
        logger.warning(f"Timeout occurred: {e}")
        logger.warning("The device may be out of range or not responding.")
        logger.info("Please check:")
        logger.info("  - Device is powered on and in range")
        logger.info("  - BLE address is correct")
        logger.info("  - No other applications are connected to the device")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Error occurred: {e}")
        import traceback
        logger.debug(traceback.format_exc())
        sys.exit(1)


def main() -> None:
    """Main entry point for the solarlife CLI."""
    parser = argparse.ArgumentParser(
        description="Solarlife MPPT BLE client CLI tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    
    # Global flags
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable debug logging"
    )
    parser.add_argument(
        "-q", "--quiet",
        action="store_true",
        help="Only show warnings and errors"
    )
    
    # Subcommands
    subparsers = parser.add_subparsers(dest="command", help="Command to execute")
    
    # scan command
    scan_parser = subparsers.add_parser("scan", help="Scan for nearby SolarLife devices")
    scan_parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="Scan timeout in seconds (default: 10.0)"
    )
    
    # read command
    read_parser = subparsers.add_parser("read", help="Read all available outputs from a device")
    read_parser.add_argument(
        "address",
        nargs="?",
        help="BLE device address (if not provided, connects to device with highest RSSI)"
    )
    read_parser.add_argument(
        "--json",
        action="store_true",
        help="Output results as JSON"
    )
    read_parser.add_argument(
        "--scan-timeout",
        type=float,
        default=10.0,
        help="Scan timeout in seconds when auto-selecting device (default: 10)"
    )
    read_parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="BLE connection timeout in seconds (default: 30). Increase if you see repeated connection failures."
    )
    
    args = parser.parse_args()
    
    # Setup logging
    setup_logging(args.verbose, args.quiet)
    
    # Execute command
    if args.command == "scan":
        asyncio.run(cmd_scan(args.timeout))
    elif args.command == "read":
        asyncio.run(cmd_read(args.address, args.json, args.scan_timeout, args.timeout))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
