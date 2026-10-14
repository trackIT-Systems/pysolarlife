# Control socket API

`solarlifed` serves a small request/response API on a Unix socket (`daemon.socket`, default `/run/solarlife/solarlife.sock` on Linux). The `solarlife` CLI is only a client of it; anything else that can write a line of JSON can be one too. Where there are no usable Unix sockets (Windows) the endpoint is a loopback TCP address, `tcp://127.0.0.1:47821`; the protocol is the same. See [platforms.md](platforms.md).

**There is no authentication.** Whoever can reach the socket can read the values and **write settings to the charge controller** (`write`). On Unix the file permissions are the only protection (`daemon.socket_mode`, 0660 by default); a TCP endpoint is open to every local user.

The API is covered by semantic versioning (see [releasing.md](releasing.md)): commands, arguments and result fields are not removed or renamed within a major version; new fields and commands can appear, so ignore keys you do not know.

## Protocol

One JSON object per line (UTF-8, terminated by `\n`), in both directions.

```
request   {"id": 1, "cmd": "read", "args": {"block": "status"}}
success   {"id": 1, "ok": true,  "result": {...}}
failure   {"id": 1, "ok": false, "error": {"type": "not_connected", "message": "..."}}
```

* `id` is any JSON value; the daemon echoes it. `args` is optional (an object).
* A connection can carry several requests, answered in order, one at a time. The daemon closes a connection that is idle for 30 s. A request line may be at most 64 KB (`bad_request` if longer), a response can be up to 16 MB.
* The device handles one request at a time, so concurrent requests queue in the daemon; one that waits more than 30 s for the connection fails with `busy`. A client should expect seconds for a live `read` and minutes for a `rescan` or a `write` of several blocks while the radio is busy; the CLI waits 60 s.
* Invalid JSON, a missing `cmd` or `args` that is not an object give `bad_request`; an unknown command `unknown_command`.

From a shell: `echo '{"cmd":"status"}' | socat - UNIX-CONNECT:/run/solarlife/solarlife.sock`. From Python, `solarlife.ipc.request(socket, cmd, args, timeout=60)` returns the `result` or raises `DaemonError` (with `.type` and `.message`) or `DaemonUnreachable`.

## Errors

`error.type` is one of:

| type | meaning | CLI exit code |
|---|---|---|
| `bad_request` | malformed request, unknown block, bad argument | 1 |
| `unknown_command` | no such command | 1 |
| `not_connected` | no device is connected, or the link dropped during the request | 1 |
| `busy` | the connection is in use by another request for more than 30 s | 1 |
| `timeout` | the device did not answer (after three attempts), or a rescan did not finish | 1 |
| `device_exception` | the device answered with a Modbus exception frame (`message` has the code) | 1 |
| `invalid_write` | the write was refused before anything was sent (unknown or read-only register, bad value, protected register without `force`) | 1 |
| `protocol_error` | the device sent something unusable (CRC, length) | 1 |
| `internal` | a bug; the daemon logs a traceback | 1 |

CLI exit codes: 0 ok, 1 the daemon answered with an error, 2 usage error (including an `invalid_write` found by the CLI itself before sending, and profile problems), 3 the daemon is unreachable.

## Commands

Examples are real answers (a test device, shortened where marked). A device is named by its **MAC address** everywhere, also on macOS.

### `status`

No arguments. The state of the connection and the polled blocks, without any radio traffic.

```json
{"version": "1.0.0", "state": "connected", "since": "2026-10-14T09:35:18.847Z", "uptime_s": 0,
 "device": {"address": "A4:C1:38:6F:0E:2B", "name": "SolarLife", "rssi": -60, "pinned": false, "connected": true},
 "blocks": {
   "status":     {"interval_s": 60,   "last_ok": "2026-10-14T09:35:19.156Z", "age_s": 0.0, "stale": false, "last_error": null, "consecutive_failures": 0},
   "parameters": {"interval_s": 3600, "last_ok": "2026-10-14T09:35:18.854Z", "age_s": 0.3, "stale": false, "last_error": null, "consecutive_failures": 0}},
 "connection": {"connect_attempts": 1, "connects": 1, "disconnects": 0, "last_error": null, "backoff_s": null},
 "scan": {"last": "2026-10-14T09:35:18.846Z", "devices": 2, "next_in_s": 3600},
 "unsolicited": 0}
```

* `state`: `starting`, `scanning`, `connecting`, `backoff` (waiting before the next connect), `connected`, `rescanning` (connected, scanning), `disconnecting`, `stopped`.
* `device` is `null` while none is selected. `pinned` is true if `ble.address` is set. `since` is the time of the connect while connected, else of the last state change.
* `blocks.*.stale`: no successful read for three poll intervals (`polling.stale_after_factor`).
* `unsolicited`: notifications that were dropped because no request was waiting for them.

### `cached`

Arguments: `block` (`all` (default), `status`, `parameters`). The last values the daemon read, **without touching the device**.

```json
{"device": "A4:C1:38:6F:0E:2B", "connected": true,
 "blocks": {"parameters": {"time": "2026-10-14T09:35:18.854Z", "age_s": 0.3, "stale": false,
   "values": [{"name": "battery_type", "friendly": "Battery type", "unit": "", "address": "0x9021", "value": "Lithium"},
              {"name": "low_voltage_protection_voltage", "friendly": "Low voltage protection", "unit": "V", "address": "0x9022", "value": 10.6}, ...]}}}
```

A block never read has `time: null` and `values: []`. `device` is the device the cached values belong to, `null` if there are none.

### `read`

Arguments: `block` (`all` (default), `status`, `parameters`) **or** `address` (a number or a string such as `"0x3032"`) with `count` (default 1). Reads from the device through the held connection, updates the cache and the outputs, and returns the values.

```json
{"device": "A4:C1:38:6F:0E:2B",
 "blocks": {"status": {"time": "2026-10-14T09:35:19.174Z",
   "values": [{"name": "equipment_id", "friendly": "Equipment ID", "unit": "", "address": "0x3030", "value": 1.0}, ...]}}}
```

With `address` the result has `range: {"address": "0x3032", "count": 2}` and `values` instead of `blocks` (raw ranges do not update the cache). A value has `name` (the identifier to use in `write`), `friendly` (the label used as MQTT key, with the unit), `unit`, `address` (hex string) and `value` (a number, a string for enumerations, a boolean for flags). Several values can share an `address` (bit fields). Errors: `not_connected`, `timeout`, `protocol_error`, `device_exception`, `busy`; `bad_request` for an unknown block, an address that is not a number or a range without known variables.

### `write`

Arguments: `values` (an object of `name: value`, required) and `force` (default false). The names are those of [parameters.md](parameters.md) and of `solarlife variables --writable`; enumerations are case insensitive.

```json
{"written": [{"name": "float_voltage", "value": 13.8}],
 "readback": {"parameters": [{"name": "float_voltage", "friendly": "Float voltage", "unit": "V", "address": "0x9026", "value": 13.8}, ...]}}
```

Everything is validated **before the first byte is sent**: an unknown or read-only name, a value that does not fit, or a protected register (`restore_system_default_values`, the `clear_*` switches, `slave_id`, `baud_rate`, `device_password`) without `force` gives `invalid_write` and nothing is written. The values are then written in groups (contiguous holding registers in one range write, switches one by one); if a group fails, the earlier groups stay written. The daemon then reads back the polled block(s) the registers belong to and returns them in `readback` (a block that could not be read is in `readback_errors`, name to message). **The device acknowledges writes it then ignores** ([quirks.md](quirks.md)): compare `readback` with what was sent.

### `reconnect`

No arguments. Drops the connection and connects again without a backoff. Answers `{"requested": true}` at once; follow it with `status`.

### `rescan`

No arguments. Scans now, waits for the scan to finish and returns what `scan` returns. The connection stays open, unless another device is clearly stronger (`ble.switch_hysteresis_db`): then the daemon switches to it. `timeout` if the scan does not finish within `ble.scan_duration_s` plus a few seconds.

### `scan`

No arguments. The result of the last scan, no radio traffic.

```json
{"time": "2026-10-14T09:35:18.846Z",
 "devices": [{"address": "A4:C1:38:6F:0E:2B", "name": "SolarLife", "rssi": -60, "samples": 5, "current": true,  "advertising": true},
             {"address": "B4:C1:38:6F:0E:2C", "name": "SolarLife", "rssi": -75, "samples": 5, "current": false, "advertising": true}]}
```

`rssi` is the mean over `samples` advertisements, best first. The connected device does not advertise: it is listed with the signal strength it had at connect, `samples: 0` and `advertising: false`.

## Stability

Within a major version commands, arguments, error types and the documented result fields stay. What is not covered: the text of `message`, the exact set of keys in `status`, and the values of the register table (`variables.py`: names and units can be corrected when a device shows that they were wrong; such a correction is noted in the changelog).
