# Documentation

Technical notes on how the SolarLife controller speaks, what was observed on real devices and what is still unknown. The code is the reference for behaviour; these pages are the reference for *why*.

| Page | Content |
|---|---|
| [ble-protocol.md](ble-protocol.md) | The BLE protocol: characteristics, Modbus frames, values, blocks, writes |
| [parameters.md](parameters.md) | The twelve controller parameters, profile files and the premade profiles |
| [advertisements.md](advertisements.md) | The advertisement of the device (company ID, name, MAC address in the manufacturer data) |
| [platforms.md](platforms.md) | Linux, macOS and Windows: what works, what differs |
| [control-socket.md](control-socket.md) | The API of `solarlifed` on its socket: commands, arguments, results, errors |
| [quirks.md](quirks.md) | Odd behaviour of the device, BlueZ and the Raspberry Pi |
| [unknowns.md](unknowns.md) | What is guessed, copied or not understood |
| [releasing.md](releasing.md) | Versioning and how a release is made |

Sources: the protocol was reverse engineered by [subDesTagesMitExtraKaese](https://github.com/subDesTagesMitExtraKaese/solarlife-mppt-ble-client), the register table comes from the manufacturer's register documentation (see the credits in the [README](../README.md)); the daemon was tried on a Raspberry Pi with a SolarLife controller.
