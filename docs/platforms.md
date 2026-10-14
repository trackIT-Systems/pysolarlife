# Platforms

`solarlifed` and `solarlife` are written for the Raspberry Pi (tsOS, Linux), but the code is portable: Bluetooth goes through [bleak](https://github.com/hbldh/bleak) (BlueZ on Linux, CoreBluetooth on macOS, WinRT on Windows). This page says what differs. The tests run on all three systems in CI against a fake backend; **Linux (a Raspberry Pi) and macOS (an Apple-silicon Mac, Python 3.14) were tried with a real controller; Windows was not** and stays unverified until someone has run it with hardware.

| | Linux (tsOS) | macOS | Windows |
|---|---|---|---|
| Scanning, connecting, polling | yes | yes (tried) | should work |
| Pairing | not needed | not needed | not needed |
| Device names (`ble.address`, MQTT topic) | MAC address | MAC address (read from the advertisement) | MAC address |
| `ble.adapter` | BlueZ adapter (`hci0`) | ignored (warning) | ignored (warning) |
| Control socket | Unix socket | Unix socket | `tcp://127.0.0.1:47821` |
| Default paths | `/etc/solarlife`, `/run/solarlife`, `/data` | `~/Library/Application Support/solarlife` | `%PROGRAMDATA%\solarlife` |

An example config for macOS with the paths and notes below is [config/solarlife.macos.yml](../config/solarlife.macos.yml). `~` is expanded in `daemon.socket` and `daemon.csv.path`.

## Bluetooth

* **No pairing.** The controller needs no pairing, key or passkey ([ble-protocol.md](ble-protocol.md)), so unlike with other devices there is no platform-specific agent.
* **Device identity.** macOS hides MAC addresses: CoreBluetooth hands out a UUID that is only valid on that Mac. The controller puts its MAC address into its advertisement ([advertisements.md](advertisements.md)), and the daemon reads it from there, so `ble.address`, `scan`, the CSV and the MQTT topic show the same MAC address on every system. The fallback, if the manufacturer data is not six bytes long, is the address the system reports.
* **Permissions.** macOS asks for Bluetooth permission for the terminal or app that runs the daemon on the first scan (System Settings > Privacy & Security > Bluetooth).
* **`ble.adapter`** selects a BlueZ adapter and is ignored elsewhere (with a warning).
* **The Wi-Fi/Bluetooth coexistence problem** of the Raspberry Pi ([quirks.md](quirks.md)) does not apply to PCs with separate radios; connecting should be much faster and more reliable there. The long connect timeout and the backoff stay as they are.
* **Tried on macOS** (2026-10-14, Apple-silicon Mac, a device at -74 dBm): the daemon found the controller, connected at the first attempt, read the `status` and `parameters` blocks live and shut down cleanly; the scan showed the CoreBluetooth UUID while the daemon reported the MAC address from the advertisement. Writes were not tried there. The controller was not advertising while the Raspberry Pi held its connection: stop `solarlife.service` there (or any other client) before connecting from another machine; a controller takes one connection.
* **Not verified:** Windows (WinRT) in every respect, and macOS writes.

## Control socket

CLI and daemon talk newline-delimited JSON ([control-socket.md](control-socket.md)). On Linux and macOS that is a Unix socket with file permissions (`daemon.socket_mode`). Windows has no usable Unix sockets in asyncio, so the default there is loopback TCP, `daemon.socket: tcp://127.0.0.1:47821` (`solarlife --socket tcp://127.0.0.1:47821 status`). Only IPv4 loopback addresses are accepted. There is **no authentication**: every local user can read values and write settings. Use a Unix socket where there is one; on a shared Windows machine think twice.

## Smaller differences

* The daemon stops on SIGTERM, SIGINT (Ctrl+C) and, on Windows, Ctrl+Break.
* CSV file names contain no colons (Windows does not allow them).
* There is no systemd unit or boot configuration outside tsOS; run the daemon as a service of your choice (launchd, Windows Service, NSSM, ...).
