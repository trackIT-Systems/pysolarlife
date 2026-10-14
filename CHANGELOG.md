# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), the versioning follows [Semantic Versioning](https://semver.org/). Releases are made by pushing a tag `vX.Y.Z` (see [docs/releasing.md](docs/releasing.md)).

## [Unreleased]

### Changed

- The default config file on Linux is `/etc/solarlife/solarlife.yml` (was `/boot/firmware/solarlife.yml`). A system that keeps the file elsewhere passes `--config`.

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

- [#2](https://github.com/trackIT-Systems/pysolarlife/issues/2), `Bluetooth: hci0: Opcode 0x2005 failed: -16` in the kernel log while scanning, is **not fixed**: bleak's passive scanning needs kernel-level advertisement filtering that is not available. The daemon scans far less than the old client did (a scan of `ble.scan_duration_s` at `ble.rescan_interval_s`, 3600 s by default, while connected), so the messages are rare: on the test Raspberry Pi they appear about once per rescan.

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

[1.0.0]: https://github.com/trackIT-Systems/pysolarlife/releases/tag/v1.0.0
[0.3.0]: https://github.com/trackIT-Systems/pysolarlife/releases/tag/v0.3.0
[0.2.0]: https://github.com/trackIT-Systems/pysolarlife/releases/tag/v0.2.0
[0.1.0]: https://github.com/trackIT-Systems/pysolarlife/releases/tag/v0.1.0
