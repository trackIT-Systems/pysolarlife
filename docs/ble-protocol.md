# The BLE protocol

The SolarLife MPPT controllers speak **Modbus RTU frames over a BLE characteristic pair**. The protocol was reverse engineered by [subDesTagesMitExtraKaese](https://github.com/subDesTagesMitExtraKaese/solarlife-mppt-ble-client); the register table (`src/solarlife/variables.py`) was imported from the manufacturer's register documentation. The code is the reference for behaviour: `protocol.py` (frames), `session.py` (one link), `registers.py` (blocks, write checks).

## Finding the device

The device advertises with company ID `0x03A0` and the name `SolarLife`; the six bytes of manufacturer data are its MAC address ([advertisements.md](advertisements.md)). It advertises no service UUIDs. **A connected device stops advertising**, so a scan cannot see the device the daemon holds.

## Characteristics

| UUID | Use |
|---|---|
| `0000ff01-0000-1000-8000-00805f9b34fb` | notify: the device's responses |
| `0000ff02-0000-1000-8000-00805f9b34fb` | write: requests |

There is no handshake, key or pairing: enable notifications on `ff01`, write a request to `ff02`, wait for the response.

## Frames

Modbus RTU: `[device id][function code][data...][CRC16]`, the CRC is the Modbus CRC-16 (polynomial `0xA001`, start value `0xFFFF`) in **little endian** order (`crc.py`). Registers are 16 bit words, **big endian** in the frame.

| Function | Name in `variables.py` | Used for |
|---|---|---|
| `0x02` | read status register | discrete inputs `0x2000`, `0x200C` |
| `0x03` | read parameter | holding registers `0x8FF0` to `0x906A` |
| `0x04` | read memory | input registers `0x3000` to `0x316C` |
| `0x05` | write status register | the switches `0x0000` to `0x000C` |
| `0x06` | write memory single | one holding register |
| `0x10` | write memory range | several contiguous holding registers |

```
read request     FE 04 30 30 00 29 CRC        device id FE, function 04, start 0x3030, 0x29 = 41 registers
read response    01 04 52 <82 bytes> CRC      real device id, function, byte count, data
write request    id 10 90 24 00 02 04 <4 bytes> CRC      range write of 0x9024..0x9025 (byte count 04)
write response   id 10 90 24 00 02 CRC        (8 bytes) address and count echoed
single write     id 06 90 26 05 64 CRC        answered by an echo of the request (8 bytes)
switch write     id 05 00 08 FF 00 CRC        on = FF00, off = 0000; echoed
exception        id 84 02 CRC                 function | 0x80, exception code (5 bytes)
```

* **Device id.** Reads are sent to `0xFE` (any device); every response carries the device's real id (`slave_id`, register `0x9020`), which the session keeps and uses in write requests.
* **A read must not span function codes**, and the range must contain at least one known variable (`get_read_command`).
* **Responses can be split** over several notifications (a notification carries at most the negotiated payload, 20 bytes by default; the test device splits responses that way). `DeviceSession` collects chunks until the frame is complete; bytes after a complete frame are ignored.
* **One request at a time.** A notification that arrives while nothing is pending, or whose function code does not match the pending request, is dropped and counted (`unsolicited` in `status`).
* **Retries.** A request that times out (2 s by default), has a wrong CRC or fails to be sent is repeated, three attempts in all. Partial data is never returned: a read either returns every register or raises.
* **Exceptions.** An exception frame raises `DeviceException(code)`; the code is the Modbus exception code (`0x02` illegal data address, ...). The protocol layer does not interpret it.

## Values

* A register is one 16 bit word. **32 bit values take two registers, the low word first** (`value = word0 | word1 << 16`; for signed values the high word is signed).
* A variable with a multiplier is divided by it (`100`: 0.01 steps, so raw 1360 is 13.6 V).
* Enumerations and bit fields are decoded by a function per variable. Several variables can share one register as bit fields (for example `battery_temperature_protection_status` and `battery_voltage_protection_status` both live at `0x3033`); each has its own entry in a `read` result with the same `address`.
* Switches (function `0x05`) are written as `0xFF00` (the first word of `binary_payload`, "on") or `0x0000`; as holding registers they are `1` and `0`.

## Blocks

The daemon polls two blocks, which a real device answers in full:

| Block | Start | Registers | Function | Content |
|---|---|---|---|---|
| `status` | `0x3030` | 41 | `0x04` | equipment id, run days, battery voltage level, protection flags, charge state, battery / load / solar panel voltage, current and power, temperatures, daily and total energy, counters (44 values from the 41 registers: some carry bit fields) |
| `parameters` | `0x9021` | 12 | `0x03` | the twelve settings of [parameters.md](parameters.md) |

Everything else in the table can be read with `solarlife read --range ADDR:COUNT`: the rated values (`0x3000`...), the controller's functional status flags and the limits of the settings (`0x3011`...`0x302C`, also at `0x8FF0`...`0x900C`), the real-time clock and the time-control and dimming settings of the other controller series (`0x9017`...`0x906A`).

## Writing

* A **range write** (function `0x10`) needs contiguous holding registers and at most 255 data bytes; `plan_writes` groups the values that way. Switch registers (function `0x05`) are written one by one.
* The daemon checks names and values before it sends anything (`registers.build_write`): unknown and read-only registers are refused, enumerations must match, numbers must fit, and the registers that can erase data or lock the device out (`restore_system_default_values`, the `clear_*` switches, `slave_id`, `baud_rate`, `device_password`) need `force`.
* After a write the daemon reads the touched block back and returns it, so the caller sees what the device took. The device does not always take what it acknowledged, see [quirks.md](quirks.md).
