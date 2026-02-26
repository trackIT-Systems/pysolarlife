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
        stream=sys.stdout
    )


async def cmd_scan(timeout: float) -> None:
    """Scan for SolarLife devices and print them as they appear."""
    logging.info("Scanning for SolarLife MPPT devices (Manufacturer ID 0x03A0)...")
    
    def on_found(device, advertisement_data):
        """Print device immediately when discovered."""
        name = device.name or "(no name)"
        rssi = advertisement_data.rssi if advertisement_data.rssi is not None else "N/A"
        if rssi != "N/A":
            logging.info(f"{device.address}  {name}  RSSI: {rssi}dBm")
        else:
            logging.info(f"{device.address}  {name}")
    
    devices = await MpptClient.scan(timeout=timeout, filter_devices=True, on_found=on_found)
    
    if not devices:
        logging.warning("No SolarLife devices found.")


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
        logging.info(f"Scanning for SolarLife devices (timeout: {scan_timeout}s)...")
        devices = await MpptClient.scan(timeout=scan_timeout, filter_devices=True)
        
        if not devices:
            logging.error("No SolarLife devices found during scan.")
            sys.exit(1)
        
        # Find device with highest RSSI
        best_device = None
        best_rssi = float('-inf')
        for device, ad_data in devices:
            rssi = ad_data.rssi if ad_data.rssi is not None else float('-inf')
            if rssi > best_rssi:
                best_rssi = rssi
                best_device = device
        
        if best_device is None:
            logging.error("No devices with RSSI information found.")
            sys.exit(1)
        
        device_address = best_device.address
        logging.info(f"Connecting to device with highest RSSI: {device_address} ({best_rssi}dBm)")
    
    # Connect and read data
    charger = None
    try:
        charger = MpptClient(device_address, timeout=timeout)
        async with charger:
            # Read both status and parameters
            status = await charger.read_status()
            parameters = await charger.read_parameters()
            
            # Combine results
            all_results = status + parameters
            
            if not all_results:
                logging.warning("No data received.")
                return
            
            if json_output:
                # Format as JSON with composite keys
                output = {}
                for result in all_results:
                    if result.unit:
                        key = f"{result.friendly_name} ({result.unit})"
                    else:
                        key = result.friendly_name
                    output[key] = result.value
                print(json.dumps(output, indent=2))
            else:
                # Format as text table
                for result in all_results:
                    unit = f" {result.unit}" if result.unit else ""
                    logging.info(f"{result.friendly_name}: {result.value}{unit}")
                    
    except TimeoutError as e:
        logging.warning(f"Timeout occurred: {e}")
        # Try to show partial data if available
        if charger and hasattr(charger, 'buffer') and charger.buffer:
            logging.debug(f"Partial data received before timeout: 0x{charger.buffer.hex()}")
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
        sys.exit(1)
    except Exception as e:
        logging.error(f"Error occurred: {e}")
        import traceback
        logging.debug(traceback.format_exc())
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
