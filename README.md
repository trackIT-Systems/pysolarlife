# solarlife

`solarlifed` is a daemon that keeps a Bluetooth Low Energy connection to a SolarLife MPPT solar charge controller, polls its registers and reports them to a CSV file and to MQTT (JSON and CBOR). `solarlife` is a command line tool that talks to the running daemon: cached and live values, writes, reconnect, scan.

> **The BLE protocol was reverse engineered. Writing registers can change charging behaviour and erase statistics. Use `write` only if you understand what a register does.**

## Why a daemon

Opening a BLE connection is the unreliable step, especially on a Raspberry Pi whose Wi-Fi access point shares the radio with Bluetooth (one chip, one 2.4 GHz antenna). A connection that is already open is stable. So the daemon opens one connection, keeps it, and polls through it, instead of connecting for every reading.

* It scans for SolarLife devices (manufacturer ID `0x03A0`) and connects to the one with the strongest signal. A rescan runs at a long interval while connected and switches device only if another one is clearly stronger (`switch_hysteresis_db`). `ble.address` pins a device.
* Scanning and connecting never overlap in the daemon: BlueZ rejects a connect while a scan runs (`org.bluez.Error.InProgress`). Failed connects are retried with a jittered, growing delay, and each retry scans first, because BlueZ forgets a device after a failed connect.
* The `status` registers (`0x3030`, 41 registers) and the `parameters` registers (`0x9021`, 12 registers) are polled on separate intervals, and both are read right after every (re)connect. After `reconnect_after_failures` failed reads in a row the link is dropped and reopened.
* A read never returns partial or empty data: it raises. Notifications nobody asked for are counted and ignored.
* A connected SolarLife stops advertising, so rescans cannot see it. The signal strength recorded at connect is used for the comparison.

## Install

Python 3.12+, `bleak`, `paho-mqtt`, `cbor2`, `PyYAML` (all available as Debian `python3-*` packages). Releases are on the [releases page](https://github.com/trackIT-Systems/pysolarlife/releases) (wheel and source archive; see [CHANGELOG.md](CHANGELOG.md)):

```bash
pip install https://github.com/trackIT-Systems/pysolarlife/releases/download/v1.0.0/solarlife-1.0.0-py3-none-any.whl
```

From a checkout:

```bash
pip install -e ".[dev]"
pytest
```

## Platforms

Linux (tsOS, a Raspberry Pi) is the system it was built and tried on. macOS was tried with a real controller (connect, live reads; no writes) and Windows was not; both work through bleak (CoreBluetooth, WinRT) and are tested against fakes in CI; see [docs/platforms.md](docs/platforms.md) for what differs (paths, the control socket on Windows, device names on macOS). An example config for macOS is [config/solarlife.macos.yml](config/solarlife.macos.yml).

## Daemon

```bash
sudo install -D config/solarlife.yml /etc/solarlife/solarlife.yml
solarlifed                                   # reads /etc/solarlife/solarlife.yml
solarlifed --config ./config/solarlife.yml -v
```

See [config/solarlife.yml](config/solarlife.yml) for all options (the default config path, socket and CSV directory depend on the system, see [docs/platforms.md](docs/platforms.md)). On Linux it needs access to BlueZ over D-Bus and write access to the CSV directory. SIGTERM disconnects cleanly and publishes the state `offline`.

### Output

Every block read (`status` once a minute, `parameters` once an hour) and every connection event go out over **MQTT** as CSV, JSON and CBOR, on `{host}/solarlife/{mac}/{block}/{csv,json,cbor}`, and into **CSV files**, one per block and day: `<csv.path>/<host>/solarlife/<block>/<host>_<date>_solarlife-<block>.csv`.

- **Columns:** all formats carry the same ones: `time` (with UTC offset), `synced`, `device`, then the block's values under their names (`battery_voltage`, `solar_panel_power`, ...).
- **CBOR** is a list in column order, about 200 bytes per `status` reading, for cellular links.
- **Rate limit:** MQTT sends at most one reading per device and block every `mqtt.publish_interval_s` (300 s), always the newest. The files get every reading.
- **Clock:** readings taken before the clock is synced are re-timed before they reach the files.
- **State:** `{host}/solarlife/state/{json,cbor}` is retained, with `offline` as the last will.

Topics, columns with units, examples and the clock handling: [docs/outputs.md](docs/outputs.md).

## CLI

```
solarlife status                       state, device, age of the last reads, connect statistics
solarlife cached [status|parameters]   last values the daemon read (no radio traffic)
solarlife read [status|parameters]     live read through the held connection
solarlife read --range 0x3032:2        raw registers
solarlife write float_voltage=13.6     validated, confirmed, read back (--yes, --force)
solarlife profile save|load|diff|show|list   parameter sets, see below
solarlife reconnect                    drop the connection and connect again
solarlife rescan                       scan now and re-select the device
solarlife scan                         last scan results
solarlife variables [--writable]       known variables (works without the daemon)
```

`--json` prints the raw result, `--socket` / `--config` select the daemon. Exit codes: 0 ok, 1 daemon error, 2 usage error, 3 daemon unreachable.

`write` checks names and values before anything is sent, refuses read-only variables, and needs `--force` for variables that can erase data or lock the device out (restore/clear switches, slave id, baud rate, password). Registers that are not adjacent are written separately.

### Profiles

A profile is a YAML file with a set of the twelve controller parameters (battery type, voltage thresholds, ...). A few 12 V profiles are built in.

```
solarlife profile save before.yml      save the current parameters
solarlife profile diff agm-12v         what would change
solarlife profile load agm-12v         diff, confirm, write only what differs, read back and verify
solarlife profile load before.yml -y   restore
```

All parameters, accepted values and the file format are in [docs/parameters.md](docs/parameters.md). The built-in voltages are typical values: check them against your battery's datasheet.

The CLI and the daemon talk newline-delimited JSON over a Unix socket (`daemon.socket`; loopback TCP on Windows): `{"id": 1, "cmd": "read", "args": {"block": "status"}}` answers `{"id": 1, "ok": true, "result": ...}` or `{"id": 1, "ok": false, "error": {"type": "not_connected", "message": ...}}`. The commands, arguments, results and errors are documented in [docs/control-socket.md](docs/control-socket.md). **There is no authentication**: whoever can reach the socket can write settings.

## Troubleshooting

**Connecting takes minutes, or fails with `connect timed out`, `br-connection-canceled`, `failed to discover services, device disconnected`.** On a Raspberry Pi with a 2.4 GHz access point up, Bluetooth and Wi-Fi compete for the radio and opening a connection often fails, while the same device connects immediately from a phone. An open connection is not affected, which is why the daemon holds it. Moving the access point to 5 GHz fixes it where the hardware supports that. The connect timeout is long on purpose (45 s): BlueZ retries internally and a connect often succeeds after 10-40 s.

## Documentation

[docs/](docs/README.md): the BLE protocol, the parameters and profiles, the advertisement, platforms, the control socket API, quirks and open questions, releasing. [CHANGELOG.md](CHANGELOG.md) lists the changes of every release.

## Layout

`src/solarlife/`: `protocol.py`, `variables.py`, `crc.py` (the register protocol), `session.py` (one held link), `manager.py` (scan and connect state machine), `poller.py`, `registers.py` (blocks and write validation), `profiles.py` (parameter sets, `premade/`), `outputs/`, `server.py` and `ipc.py` (control socket), `platform.py` (per-system defaults), `daemon.py`, `cli.py`, `ble/` (transport interface; only `ble/bleak_backend.py` imports bleak).
`tests/` runs without BLE hardware against a fake backend (`tests/fakes.py`).

## Credits

The BLE protocol was reverse engineered by [subDesTagesMitExtraKaese](https://github.com/subDesTagesMitExtraKaese/solarlife-mppt-ble-client), whose client this project started from (the register table, `src/solarlife/variables.py`, comes from the manufacturer's register documentation as imported there). Further fixes to the calculations came from David Dubrović (pull request 3 of that project).

## License

MIT
