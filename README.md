# solarlife

A Python library for reading and controlling Solarlife MPPT solar charge controllers over Bluetooth Low Energy (BLE).

## Requirements

- Python 3.10+
- [Bleak](https://github.com/hbldh/bleak) — BLE library for Python

## Installation

```bash
git clone https://github.com/subDesTagesMitExtraKaese/solarlife-mppt-ble-client.git
cd solarlife-mppt-ble-client
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Quick start

```python
import asyncio
from solarlife import MpptClient

async def main():
    async with MpptClient("AA:BB:CC:DD:EE:FF") as charger:
        status = await charger.read_status()
        print(f"Battery: {status['battery_percentage'].value}%")
        print(f"Voltage: {status['battery_voltage'].value} V")
        print(f"Solar power: {status['solar_panel_power'].value} W")

asyncio.run(main())
```

## API

### `MpptClient(mac_address, timeout=30.0)`

Async context manager that connects to the charge controller over BLE.

- `mac_address` — BLE address of the device (e.g. `"AA:BB:CC:DD:EE:FF"`)
- `timeout` — seconds to wait for the connection to be established (default: `30.0`). BlueZ uses this budget for its internal retries when the connection is aborted by the local adapter. Increase this value if you experience frequent timeouts.

```python
async with MpptClient("AA:BB:CC:DD:EE:FF") as charger:
    ...
```

#### Reading data

```python
# High-level convenience reads
status = await charger.read_status()      # real-time data (0x3030, 41 registers)
params = await charger.read_parameters()  # battery/load parameters (0x9021, 12 registers)

# Generic read — any register range
data = await charger.read(start_address=0x3000, count=10)
```

#### Writing data

```python
from solarlife import Result, variables

variable = variables["float_voltage"]
result = Result(**vars(variable), value=13.6)
await charger.write([result])
```

#### Accessing results

```python
# By name
print(status["battery_voltage"].value)

# By index
print(status[0].value)

# Iterate
for result in status:
    unit = f" {result.unit}" if result.unit else ""
    print(f"{result.friendly_name}: {result.value}{unit}")
```

### `MpptClient.scan()`

Static method — scans for nearby BLE devices:

```python
devices = await MpptClient.scan()
for device in devices:
    print(device.address, device.name)
```

## Variables

The `variables` container holds all known register definitions for the controller:

```python
from solarlife import variables

for var in variables:
    print(var.name, hex(var.address), var.unit)

# Look up by name
var = variables["battery_voltage"]
```

Each `Variable` has:

| Field | Description |
|---|---|
| `address` | Register address |
| `name` | Key name (snake_case) |
| `friendly_name` | Human-readable label |
| `unit` | Unit of measurement (`"V"`, `"A"`, `"W"`, …) |
| `multiplier` | Scaling factor applied to the raw register value |
| `is_32_bit` | Whether the register spans two 16-bit words |
| `is_signed` | Whether the value is signed |
| `func` | Optional decoding function for enum-like values |
| `binary_payload` | `(on_value, off_value)` tuple for switch registers |

## Example script

```bash
# Scan for devices
python examples/read_all.py --scan

# Read all status registers from a device
python examples/read_all.py AA:BB:CC:DD:EE:FF
```

## Troubleshooting

### Connection timeouts on Raspberry Pi (or other single-chip WiFi+BT devices)

**Symptom:** The connection fails repeatedly with messages like `le-connection-abort-by-local` or a timeout error, even though the device is clearly visible during the scan. The same device connects instantly from a mobile phone.

**Root cause:** Raspberry Pi 4/5 (and similar boards) use a Broadcom/Cypress combo chip (BCM4345) that shares a single 2.4 GHz antenna between WiFi and Bluetooth. When a 2.4 GHz WiFi access point is active on that chip, it monopolises the radio and the BLE firmware returns `-EBUSY` on every connection attempt. The kernel will log this as:

```
Bluetooth: hci0: Opcode 0x2006 failed: -16
```

**Fix:** Move the WiFi AP to the 5 GHz band. WiFi at 5 GHz and Bluetooth operate in completely separate spectrum and will no longer interfere. With `nmcli`:

```bash
sudo nmcli con modify <hotspot-connection-name> \
    802-11-wireless.band a \
    802-11-wireless.channel 36
sudo nmcli con up <hotspot-connection-name>
```

Or set `hw_mode=a` and an appropriate `channel` in your `wpa_supplicant` / `hostapd` configuration.

**Workaround (if 5 GHz is not available):** Increase the connection timeout so BlueZ has a longer window to succeed between WiFi transmissions:

```bash
solarlife read AA:BB:CC:DD:EE:FF --timeout 60
```

Or in code:

```python
async with MpptClient("AA:BB:CC:DD:EE:FF", timeout=60.0) as charger:
    ...
```

## License

MIT
