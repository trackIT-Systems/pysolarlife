# What solarlifed publishes

`solarlifed` publishes every block it reads, and its connection events, over MQTT and into CSV files. Both use the same columns. This page is the reference for consumers: topics, encodings, columns, rate limit and files.

## MQTT

| Topic | Payload | Retained | Rate |
|---|---|---|---|
| `{host}/solarlife/{mac}/{block}/csv` | one `;`-separated line, no header | no | at most one per `mqtt.publish_interval_s` per device and block |
| `{host}/solarlife/{mac}/{block}/json` | object, keys in column order | no | as above |
| `{host}/solarlife/{mac}/{block}/cbor` | array, values in column order | no | as above |
| `{host}/solarlife/events/{csv,json,cbor}` | one connection event | no | every event |

- `{host}` is the host name. `{mac}` is the controller's address without colons, in lower case, e.g. `a4c1386f0e2b`.
- `{block}` is `status` (read every `polling.status_interval_s`, 60 s by default) or `parameters` (every `polling.parameters_interval_s`, one hour).
- `mqtt.csv`, `mqtt.json` and `mqtt.cbor` switch the formats on and off.
- QoS is `mqtt.qos` (0 by default). A message published while the broker is unreachable is dropped.
- Nothing carries a time and nothing is retained: a reading is a snapshot of now, and the receiver stamps it on arrival. A reading that arrives is current (at most `publish_interval_s` plus the block's interval old); one that does not is lost. The station's time and whether it can be trusted are in tstelemetry's `timesync` readings (tsOS-base docs/time.md).

### Encodings

All three formats of a reading carry the same columns in the same order:

1. `device`: the controller's address, e.g. `A4:C1:38:6F:0E:2B`.
2. The block's columns, listed below. A value the reading lacks is `null` (empty in CSV).

CBOR has no key names and writes each number in the smallest form that keeps it exact. That makes it the smallest format, for cellular links. A `status` reading is about 175 bytes as CBOR, 240 as CSV and 1450 as JSON. Decode a CBOR array by the column order of its block below; the CSV files have the same order after `time`.

A `status` reading as CSV:

```
A4:C1:38:6F:0E:2B;1.0;0.0;24.0;Normal;Normal;False;True;False;Not charging;False;False;Light load;False;False;False;False;False;True;25.0;29.0;0.0;0.0;0.0;0.0;0.0;0.0;0.0;0.0;0.0;31.0;23.4;0.0;0.0;23.4;0.0;0.0;5.8;0.0;0.0;0.0;0.0;0.0;0.0;0.0
```

and the start of the same reading as JSON:

```json
{"device": "A4:C1:38:6F:0E:2B", "equipment_id": 1.0, "run_days": 0.0, "battery_voltage_level": 24.0, "battery_temperature_protection_status": "Normal", "battery_voltage_protection_status": "Normal", "…": "…"}
```

### Compatibility

From 1.1.0 on and within a major version, columns are only appended to a block: none is removed, renamed or moved, and none changes its unit or meaning. A new column is a minor version (see [releasing.md](releasing.md)). The same holds for the event columns, and for event names and `detail` keys, which are only added.

The messages carry no schema version, because the number of columns tells which ones there are: a CBOR array of n values holds the first n columns of its block below. A consumer decodes by position, ignores values beyond the columns it knows, and treats columns missing from a shorter array (from an older daemon) as null; JSON consumers ignore unknown keys. In the CSV files, a daemon that is updated during the day appends longer rows to a file whose header has fewer columns; the header is a prefix of the newer columns.

### Rate limit

`mqtt.publish_interval_s` (300 s by default) allows at most one MQTT message per device and block in that time.
- The first reading goes out at once.
- Readings within the interval are held back; only the newest is kept, and it goes out when the interval ends.
- Nothing is sent twice.
- With `0`, every reading goes out.

Events are not limited, and neither are the CSV files: they get every reading taken once the clock is trusted.

### Events

Columns: `device` (or null), `event`, `detail`; the files add `time` first. `detail` is the event's data as `key=value` pairs, e.g. `rssi=-67 attempts=3 name=SolarLife`.

Events: `connected`, `connect_failed`, `disconnected`, `device_switched`, `scan_completed`, `read_failed`, `daemon_started`, `daemon_stopped`.

### Bridging over cellular

Forward the CBOR topics; leave CSV and JSON for local consumers:

```
topic +/solarlife/+/+/cbor out 1
```

This matches the readings only (`{host}/solarlife/{mac}/{block}/cbor`). Add `topic +/solarlife/events/cbor out 1` if the events are wanted too. There is no state topic: a controller or daemon that stops shows as missing readings, and the connection's changes are events (`solarlife status` shows the current state on the station).

## Columns

### `status`

| Column | Unit | Meaning |
|---|---|---|
| `equipment_id` |  | Equipment ID |
| `run_days` |  | Number of running days |
| `battery_voltage_level` | V | Current battery voltage level |
| `battery_temperature_protection_status` |  | Battery temperature protection status |
| `battery_voltage_protection_status` |  | Battery voltage protection status |
| `solar_panel_charge_disabled` |  | Is charging manually disabled |
| `solar_panel_is_night` |  | Is solar panel night |
| `solar_panel_charge_over_temperature` |  | Is charge over temperature |
| `solar_panel_charge_state` |  | Solar panel charge status |
| `solar_panel_charge_fault` |  | Is charge fault |
| `solar_panel_is_charging` |  | Solar panel is charging |
| `load_state` |  | Load status |
| `output_short_circuit` |  | Is output short circuit |
| `output_hardware_protection` |  | Is output hardware protection |
| `output_open_circuit_protection` |  | Is output open circuit protection |
| `output_over_temperature` |  | Is output over temperature |
| `output_fault` |  | Is output fault |
| `load_is_enabled` |  | Is load enabled |
| `environment_temperature` | ℃ | Environment temperature |
| `device_built_in_temperature` | ℃ | Device built-in temperature |
| `battery_empty_times` |  | Battery empty times |
| `battery_full_times` |  | Battery full times |
| `over_voltage_protection_times` |  | Over-voltage protection timess |
| `over_current_protection_times` |  | Over-current protection timess |
| `short_circuit_protection_times` |  | Short-circuit protection timess |
| `open_circuit_protection_times` |  | Open-circuit protection timess |
| `hardware_protection_times` |  | Hardware protection timess |
| `charge_over_temperature_protection_times` |  | Charge over-temperature protection timess |
| `discharge_over_temperature_protection_times` |  | Discharge over-temperature protection times |
| `battery_percentage` | % | Battery remaining capacity |
| `battery_voltage` | V | Battery voltage |
| `battery_current` | A | Battery current |
| `battery_power` | W | Battery power |
| `load_voltage` | V | Load voltage |
| `load_current` | A | Load current |
| `load_power` | W | Load power |
| `solar_panel_voltage` | V | Solar panel voltage |
| `solar_panel_current` | A | Solar panel current |
| `solar_panel_power` | W | Solar panel power |
| `solar_panel_daily_energy` | kWh | Daily solar panel energy |
| `solar_panel_total_energy` | kWh | Total solar panel energy |
| `load_daily_energy` | kWh | Daily load energy |
| `load_total_energy` | kWh | Total load energy |
| `total_light_time_during_the_day` | min | Total light time during the day |

### `parameters`

| Column | Unit | Meaning |
|---|---|---|
| `battery_type` |  | Battery type |
| `low_voltage_protection_voltage` | V | Low voltage protection |
| `low_voltage_recovery_voltage` | V | Low voltage recovery |
| `boost_voltage` | V | Boost voltage |
| `equalizing_voltage` | V | Equalizing voltage |
| `float_voltage` | V | Float voltage |
| `system_rated_voltage_level` |  | System rated voltage level |
| `charge_target_voltage_for_lithium` | V | Charge target voltage for lithium |
| `charge_recovery_voltage_for_lithium` | V | Charge recovery voltage for lithium |
| `charging_at_zero_celsius` |  | 0°C charging |
| `mt_series_load_mode` |  | Load mode for MT series controller |
| `mt_series_manual_control_default` |  | MT Series manual control mode default setting |

## CSV files

- `<csv.path>/<host>/solarlife/<block>/<host>_<YYYY-MM-DD>_solarlife-<block>.csv`: one file per block and calendar day (local time), with `time` (when the block was read, whole seconds, ISO 8601 with the station's UTC offset), then the MQTT columns.
- `<csv.path>/<host>/solarlife/events/<host>_<YYYY-MM-DD>_solarlife-events.csv`: the events `connected`, `disconnected` and `device_switched`.

The files are `;` separated, with the lines of the MQTT CSV after `time` and a header row at the top. They are only appended to, never rotated or deleted.

## Clock

A station can run with an untrusted clock for a while (no network, no GNSS, no checked RTC), and `solarlifed` does not wait for it. Rows for the files need a time that can be trusted, so they are only written once the clock is:

- The daemon reads the kernel's clock state with `adjtimex` (read-only, no privileges). chrony clears `STA_UNSYNC` once it has a source: NTP, GNSS, or the RTC that tsscheduled checked (tsOS-base docs/time.md). Once seen synced, the clock counts as synced for the rest of the daemon's run. Where there is no `adjtimex` (macOS, Windows), the clock always counts as synced.
- Before that, readings only go over MQTT; the files get nothing. The start and the end of this are logged once at INFO.
