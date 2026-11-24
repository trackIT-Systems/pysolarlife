# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), the versioning follows [Semantic Versioning](https://semver.org/). Releases are made by pushing a tag `vX.Y.Z` (see [docs/releasing.md](docs/releasing.md)).

## [Unreleased]

## [1.1.1] - 2026-11-24

### Fixed

- **No crash loop without a writable CSV directory.** The daemon created the CSV directory at startup and exited when it could not (`Cannot continue: [Errno 13] Permission denied: '/data/<host>'`), so a station without a data volume, where `/data` is not writable, restarted it every few seconds and got no readings over MQTT either. The directory is now made with the first row; while the files can't be written, the readings go over MQTT only, and this is logged once at WARNING when it starts and at INFO when files are written again ([docs/outputs.md](docs/outputs.md#csv-files)).

## [1.1.0] - 2026-11-18

**Breaking: the MQTT and CSV outputs are new** (see Changed and [docs/outputs.md](docs/outputs.md)). Readings and events go out with fixed columns as CSV, JSON and CBOR, without time and display texts; the retained `state` topic is gone; the CSV files are one file per block and day. Consumers of the 1.0 outputs must be adapted; tsOS brings this version with the tsOS release that announces the change. This is the one exception to semantic versioning: the CLI, the config and the control socket stay compatible with 1.0, and from 1.1.0 on the outputs are covered too: columns are only appended ([docs/releasing.md](docs/releasing.md), [docs/outputs.md](docs/outputs.md#compatibility)).

### Added

- **Passive scanning.** On Linux, scans are passive where BlueZ offers `AdvertisementMonitor1` (bluetoothd with `Experimental = true`), matching SolarLife's manufacturer data. A passive scan sends no scan requests, so it no longer needs a random address, which the Pi's controller refuses while the station advertises: the kernel logged `Bluetooth: hci0: Opcode 0x2005 failed: -16` at every search scan (tsOS-base#50). bleak reports an advertisement in a passive scan only when its data changes, and a SolarLife's never does (its MAC address); BlueZ calls `DeviceFound` on the advertisement monitor for every advert, so the daemon replaces bleak's monitor class at runtime (in its own process, bleak's files are unchanged) by one that reports each of them. Without the bleak internals this needs, `auto` scans actively. `ble.scanning` chooses: `auto` (default: passive, else active with one warning), `passive` (no fallback) or `active`. macOS and Windows always scan actively. Fixes [#2](https://github.com/trackIT-Systems/pysolarlife/issues/2).
- **MIT license holders** ([LICENSE](LICENSE)): the notice of the client this project started from stays, the rewrite's is added.
- **Coverage in CI**: Linux runs every test, the passive scan's included, and fails below 89 % coverage.

### Fixed

- **Retry delays overflowed.** After 1024 failed connects without a link that stayed up (a controller in range that cannot be connected while the Wi-Fi access point is up), the delay calculation raised `OverflowError(34, 'Numerical result out of range')` and the daemon exited. The retry count no longer grows once the delay has reached its maximum, as in pysmartsolar (pysmartsolar#4).
- **`Future exception was never retrieved` (ERROR) after a lost link.** When the link was lost while a request was being written, the pending response got the `link lost during request` error, the write failed, and nobody awaited the response any more; asyncio logged it with a traceback. The pending response is now cancelled or its error retrieved whenever a request ends.

### Changed

- **Logging.** Under systemd, `solarlifed` logs to the journal through `python3-systemd`'s `JournalHandler` (Debian package `python3-systemd`), each entry with its syslog priority, `SYSLOG_IDENTIFIER` `solarlifed`, the source location (`CODE_FILE`, `CODE_LINE`, `CODE_FUNC`), `LOGGER`, `THREAD_NAME`, `PROCESS_NAME`, `CODE_ARGS` and, with a traceback, `EXCEPTION_TEXT`; the message is `<logger>: <message>`. Before, every line went through stderr and was stored as `info`. Without `python3-systemd` (macOS) and on a terminal it logs to stderr as before (tsOS-base#44).
- **Output formats.** Every reading and event goes out over MQTT as CSV, JSON and CBOR (`mqtt.csv`, `mqtt.json`, `mqtt.cbor`). All three carry the same fixed columns: `device`, then the block's values under their names. They carry no time (the receiver stamps a reading on arrival, tsOS-base docs/time.md), and nothing is retained. The display texts with units (`"Battery voltage (V)"`) are gone from all outputs; units are documented per column in [docs/outputs.md](docs/outputs.md). CBOR is a list in column order with each number in its smallest exact form: a `status` reading is about 175 bytes instead of 1500. Readings are published on `{host}/solarlife/{mac}/{block}/{fmt}` (the block decides the columns), events on `{host}/solarlife/events/{fmt}`. The retained `state` topic and its `offline` last will are gone: a silent controller shows as missing readings, and connection changes are events.
- **MQTT rate limit.** `mqtt.publish_interval_s` (default 300 s) allows at most one reading per device and block in that time, always the newest; `0` publishes every reading. Events and files are not limited.
- **CSV files.** One file per block and calendar day, `<csv.path>/<host>/solarlife/<block>/<host>_<YYYY-MM-DD>_solarlife-<block>.csv`, with `time` (UTC offset) first, then the MQTT columns; events in `.../events/`. This replaces one long-format file per daemon start.
- **Clock.** Rows reach the CSV files only once the clock is trusted (the kernel's `adjtimex` state: chrony has a source, NTP, GNSS or the checked RTC). Before that, readings only go over MQTT; there is no spool and no re-timing.
- The default config file on Linux is `/etc/solarlife/solarlife.yml` (was `/boot/firmware/solarlife.yml`). A system that keeps the file elsewhere passes `--config`.
- **Default log level WARNING.** `daemon.log_level` defaults to `WARNING` (was `INFO`), as do the shipped configs, following tsOS's logging convention: a working station logs nothing in normal operation. `-v` still turns on DEBUG.
- **Quiet logging.** A failure that repeats on every poll or retry (a block that cannot be read, the reconnect after failed reads, a failed connect, BlueZ being busy, a stale device handle, a failed scan, an output that fails) is logged once when it starts, again only when its error changes, and once when it works again (`Reading status from ... works again`, `Connected to ...`, `Scanning works again`); the repeats are at DEBUG. State changes of the connection are at DEBUG; connected and a lost connection are logged at INFO. Configuration errors and fatal errors at startup are logged at CRITICAL through the journal instead of printed to stderr (exit code 1 as before). Loggers are named after their modules (`solarlife.daemon` instead of `solarlifed`), and messages are capitalized sentences (`Listening on the control socket ...` instead of `control socket ...`).

## [1.0.0] - 2026-10-14

A rewrite: from a library for one-shot reads to a daemon and a CLI. It breaks with the 0.x API in every way (package layout, entry points, dependencies, Python version, output format), hence 1.0.0. The development steps 0.4.0 and 0.5.0 of the rewrite were never released and are part of this release.

### Added

- `solarlifed`, a daemon that holds one BLE connection to a SolarLife MPPT charge controller and reports to CSV (`/data/<host>/solarlife/...`) and MQTT (JSON and CBOR, `{host}/solarlife/{mac}/json|cbor`, retained `state`, `events`), configured by a YAML file.
- Connection management for a radio that is shared with a Wi-Fi access point: scanning and connecting never overlap, failed connects are retried with a jittered growing delay (a scan first, because the device handle is stale), the connect timeout is long on purpose, and the device with the strongest signal is selected (`ble.address` pins one; another device must be `switch_hysteresis_db` stronger to replace it).
- Polling of the `status` (0x3030, 41 registers) and `parameters` (0x9021, 12 registers) blocks on separate intervals, right after every connect, with a reconnect after repeated failed reads and cached values with staleness.
- `solarlife` CLI over a Unix socket: `status`, `cached`, `read` (blocks or raw ranges), `write`, `profile save|load|diff|show|list`, `reconnect`, `rescan`, `scan`, `variables`. Exit codes 0 ok, 1 daemon error, 2 usage error, 3 daemon unreachable.
- Validated writes (names and values are checked before anything is sent, read-only registers are refused, restore/clear/slave id/baud rate/password need `--force`) with read-back verification.
- Parameter profiles (YAML files over the twelve controller parameters), four premade 12 V profiles, `docs/parameters.md`.
- macOS and Windows support (macOS tried with a real controller: connect and live reads; Windows only tested against fakes in CI): the BLE code only asks bleak, and macOS, which hides MAC addresses, gets the MAC address of a device from its advertisement, so that `ble.address`, `scan` and the MQTT topic mean the same everywhere (see `docs/platforms.md`).
- `daemon.socket: tcp://127.0.0.1:port` as a loopback control endpoint (the default on Windows); `--socket` takes it too.
- Per-platform default paths (config, socket, data), `~` in configured paths, shutdown on Ctrl+Break on Windows, an example config for macOS.
- Documentation of the protocol, the quirks of the device and the open questions, and the control socket API in `docs/`.
- Tests without BLE hardware (fake backend, `pytest -W error`), CI on Ubuntu, macOS and Windows (Windows reports without failing the build until it has been tried), a release workflow that builds and publishes on a tag.

### Changed

- Python 3.12 or newer (was 3.10); `cbor2`, `paho-mqtt` and `PyYAML` are new dependencies next to `bleak`; a `src` layout.
- The second "Is charge fault" variable is called `solar_panel_charge_fault` so it no longer collides with the charge state.

### Removed

- The library API (`MpptClient`, `read_device`, `read_device_async`, `read_devices`) and the one-shot `solarlife read` / `solarlife scan` commands of 0.x. The protocol layer (`solarlife.protocol`, `solarlife.variables`) stays.

### Fixed

- Decoding results to JSON failed in some cases ([#1](https://github.com/trackIT-Systems/pysolarlife/issues/1)): `'str' object has no attribute 'unit'` came from the one-shot client's dictionary conversion, which does not exist any more. Every value now goes through one `Value` type with name, unit, address and value.

### Known issues

- [#2](https://github.com/trackIT-Systems/pysolarlife/issues/2), `Bluetooth: hci0: Opcode 0x2005 failed: -16` in the kernel log while scanning, is **not fixed** (fixed in 1.1.0 by passive scanning): bleak's passive scanning needs kernel-level advertisement filtering that is not available. The daemon scans far less than the old client did (a scan of `ble.scan_duration_s` at `ble.rescan_interval_s`, 3600 s by default, while connected), so the messages are rare: on the test Raspberry Pi they appear about once per rescan.

## [0.3.0] - 2026-02-27

Reconstructed from the git history. The tags of these versions were added afterwards (`v0.1.0`, `v0.2.0`, `v0.3.0`; an older tag `0.3.0` without the `v` points to the same commit).

### Added

- `read_devices()` helper that returns all devices in range.

### Changed

- Logging goes to stderr.

### Fixed

- A memory leak: the D-Bus connections are kept between reads (2026-03-03, after the version was set).

## [0.2.0] - 2026-02-26

### Added

- `solarlife` command line tool with `scan` and `read`.
- Device scan limited to the manufacturer ID `0x03A0`, and the analysis of the advertisements (`identify_devices.md`, now `docs/advertisements.md`).

## [0.1.0] - 2026-02-23

The version of the refactoring into a package (the project began in 2023 as a fork of [solarlife-mppt-ble-client](https://github.com/subDesTagesMitExtraKaese/solarlife-mppt-ble-client), see the credits in the README).

### Added

- `solarlife` Python package with `MpptClient`, an async context manager that connects over BLE with bleak and reads the status and parameter blocks or any register range.
- Writing of registers through `Result` objects.

### Changed

- The standalone MQTT / Home Assistant program became a library; the Home Assistant bridge was removed.

[1.1.1]: https://github.com/trackIT-Systems/pysolarlife/releases/tag/v1.1.1
[1.1.0]: https://github.com/trackIT-Systems/pysolarlife/releases/tag/v1.1.0
[1.0.0]: https://github.com/trackIT-Systems/pysolarlife/releases/tag/v1.0.0
[0.3.0]: https://github.com/trackIT-Systems/pysolarlife/releases/tag/v0.3.0
[0.2.0]: https://github.com/trackIT-Systems/pysolarlife/releases/tag/v0.2.0
[0.1.0]: https://github.com/trackIT-Systems/pysolarlife/releases/tag/v0.1.0
