from __future__ import annotations


class SolarlifeError(Exception):
    """Base class for all errors raised by solarlife."""


class ConfigError(SolarlifeError):
    pass


class NotConnectedError(SolarlifeError):
    """No device is connected, or the link dropped during a transaction."""


class TransactionTimeout(SolarlifeError):
    """The device did not answer a request in time, after all retries."""


class ProtocolError(SolarlifeError):
    """A response was malformed (CRC, length, unexpected content)."""


class DeviceException(SolarlifeError):
    """The device answered with a Modbus exception frame."""

    def __init__(self, code: int) -> None:
        super().__init__(f"device returned exception code 0x{code:02x}")
        self.code = code


class InvalidWriteError(SolarlifeError):
    """A write request failed validation before anything was sent."""


class BusyError(SolarlifeError):
    """The connection is in use by another transaction."""


class ConnectError(SolarlifeError):
    """Opening the BLE connection failed."""

    def __init__(self, message: str, *, in_progress: bool = False, not_found: bool = False) -> None:
        super().__init__(message)
        self.in_progress = in_progress
        self.not_found = not_found
