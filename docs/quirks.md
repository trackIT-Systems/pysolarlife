# Quirks and special cases

Things that are surprising about the device, BlueZ or the Raspberry Pi, with how the daemon deals with them. What was only observed once or read from the code is said so.

## Wi-Fi and Bluetooth share a radio

On a Raspberry Pi Wi-Fi and Bluetooth share one chip and one 2.4 GHz antenna.

* With the 2.4 GHz access point up, **opening a connection often fails** (`connect timed out`, `br-connection-canceled`, `failed to discover services, device disconnected`) or takes minutes, while the same device connects at once from a phone. A connection that is already open is stable. This is why the daemon holds one connection instead of connecting for every reading.
* BlueZ retries a connect internally and often succeeds after 10 to 40 s. A short connect timeout cancels attempts that would have worked: the default is 45 s.
* Moving the access point to 5 GHz fixes it where the hardware supports that.
* PCs with separate radios do not have the problem ([platforms.md](platforms.md)).

## BlueZ

* **No connect during a scan.** BlueZ rejects a connect while a scan runs (`org.bluez.Error.InProgress`). One task owns scanning and connecting so they never overlap; after an `InProgress` the retry delay has a floor of `ble.inprogress_min_s`.
* **BlueZ forgets a device after a failed connect.** The handle from the scan is useless then (`device not found`), so every retry scans first, and the scan ends as soon as the wanted device is seen. A stale handle costs no backoff and no failure count.
* **Connect with the scanned device, not the address.** With an address string bleak runs its own hidden scan, which BlueZ may refuse while the daemon scans.
* **A failed or cancelled connect always ends with a disconnect**, otherwise BlueZ keeps a half-open connection that makes the next attempt fail with `InProgress`.
* `bleak` raises `EOFError` from `disconnect()` when the connection is already gone; the daemon ignores it.
* `Bluetooth: hci0: Opcode 0x2005 failed: -16` in the kernel log while scanning ([issue 2](https://github.com/trackIT-Systems/pysolarlife/issues/2)): `HCI_LE_Set_Event_Mask` fails with `EBUSY`, probably because the scan collides with other Bluetooth activity (advertising by another program). On the test Pi the entries come once per rescan; scanning has no visible problem. Passive scanning would need kernel-level advertisement filtering, which is not available.

## The device

* **A connected device does not advertise**, so `scan` cannot see it, and neither can any other machine: while one client (for example the daemon on the Pi) is connected, no other client finds the device at all. The daemon lists the connected device anyway, with the signal strength it had at connect (`advertising: false`), and compares with that value when it rescans.
* **The device silently ignores some writes.** On a controller set to `GEL`, writing `charge_recovery_voltage_for_lithium` was accepted without an error but the value stayed unchanged, while `equalizing_voltage` took effect. The lithium voltages most likely only apply when `battery_type` is `Lithium`. Always read back; `profile load` does, and reports a mismatch with exit code 1.
* **Changing `battery_type` may reset other voltages** on its own. The read-back shows what the device ended up with.
* **Protected registers.** `restore_system_default_values`, the `clear_*` switches, `slave_id`, `baud_rate` and `device_password` need `--force`; they are not part of a profile.
* **The device id.** A response carries the device's real id (`slave_id`); requests for reading use `0xFE`. Changing `slave_id` changes what the session uses for writes after its next read.
* **Names are not unique.** `battery_type` exists as a status flag (`0x3011`, "Lithium battery" or "Non Lithium battery") and as the setting (`0x9021`); `run_days` at `0x3031`, `0x309D` and `0x316C`. A write by name resolves to the writable holding register. The second "Is charge fault" is `solar_panel_charge_fault`.

## Daemon

* **Daemon task failures exit non-zero** so that systemd restarts the daemon.
* **A reconnect request from the CLI skips the backoff**, one from a failed read does not.
* **Reads and writes share the one connection.** A request that waits more than 30 s for another one fails with `busy`.
* **The cache is dropped when the daemon switches to another device**, so values of two devices are never mixed.
* **MQTT topics use the MAC address** without colons, lower case (`{host}/solarlife/a4c1386f0e2b/status/json`). On macOS the MAC address comes from the manufacturer data of the advertisement ([advertisements.md](advertisements.md)).
* **CSV file names** have no colons (`<host>_2026-10-14_solarlife-status.csv`) because Windows does not allow them.
