# Open questions

What is guessed, copied or not understood, so nobody has to rediscover that.

## The register table

* The table (`variables.py`, 260 entries) was **imported from the manufacturer's documentation**, not measured. Only the two polled blocks (`status`, `parameters`) are known to match a real device (the sample words in the tests are a real read). Other registers can be wrong or missing; the code does not check what a device actually has: the "functional status" flags (`0x3011` to `0x3013`) tell which settings a controller offers, but nothing uses them yet.
* **Accepted ranges of the settings are unknown.** The protocol does not say which values a device accepts: the limits registers (`lvd_min_setting_value`, ...) exist but are not used to validate writes. A rejected value shows as an exception frame or a read-back mismatch.
* A **commented-out** entry `controller_functional_status_4` (`offset + 3`) is in the table without a decoding.
* **Other controller series.** The table names MT, DC and SMR series controllers (`controller_series`), with their own time-control and dimming registers (`0x902D`...`0x9054`). Only an MT-series device (the one in the tests) was seen; DC and SMR registers were never exercised.

## Advertisements

* That the six bytes of manufacturer data are the MAC address was seen on one device (`10:23:81:0C:0D:C6`). The macOS support relies on it for every device; the daemon falls back to the system's address if the data is not six bytes long.
* **The service UUID** of the two characteristics (`ff01` and `ff02`) is not recorded; the code uses the characteristics directly.

## The protocol

* **Why a device id of `0xFE` works for reads** (broadcast in Modbus terms) is not known; it is what the original client did.
* **The meaning of the Modbus exception codes** the device sends is not collected: `DeviceException` carries the code, nobody mapped them to causes.
* **Silent ignores** (see [quirks.md](quirks.md)): which writes are ignored in which state was only seen for the lithium voltages with `GEL`. Others may exist.
* **The maximum request rate** is not known; the daemon reads at most one request at a time.

## Platforms

* **Windows was never tried with a real controller**, and writes were not tried on macOS. The tests run against fakes on all three systems. Open: whether WinRT delivers notifications in the same chunks and whether a connect is quick there.
* **Passive scanning** ([issue 2](https://github.com/trackIT-Systems/pysolarlife/issues/2)) is not used.
