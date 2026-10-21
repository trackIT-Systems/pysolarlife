from __future__ import annotations

from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

from solarlife.errors import ConfigError
from solarlife.platform import DEFAULTS

DEFAULT_CONFIG_PATH = DEFAULTS.config
DEFAULT_SOCKET_PATH = DEFAULTS.socket  # a path, or tcp://127.0.0.1:port where there are no Unix sockets
DEFAULT_DATA_DIR = DEFAULTS.data_dir


@dataclass(frozen=True)
class BleConfig:
    adapter: str | None = None
    address: str | None = None
    scan_duration_s: float = 10.0
    search_interval_s: float = 60.0
    rescan_interval_s: float = 3600.0
    switch_hysteresis_db: float = 10.0
    adv_max_age_s: float = 20.0
    connect_timeout_s: float = 45.0
    backoff_initial_s: float = 2.0
    backoff_max_s: float = 30.0
    inprogress_min_s: float = 5.0
    stable_after_s: float = 60.0
    max_connect_failures: int = 10
    request_timeout_s: float = 2.0
    request_attempts: int = 3


@dataclass(frozen=True)
class PollingConfig:
    status_interval_s: float = 60.0
    parameters_interval_s: float = 3600.0
    reconnect_after_failures: int = 3
    stale_after_factor: float = 3.0


@dataclass(frozen=True)
class CsvConfig:
    enabled: bool = True
    path: Path = DEFAULT_DATA_DIR


@dataclass(frozen=True)
class MqttConfig:
    enabled: bool = True
    host: str = "localhost"
    port: int = 1883
    qos: int = 0
    csv: bool = True
    json: bool = True
    cbor: bool = True
    # At most one reading per device and block in this many seconds, the newest; 0 publishes every reading
    publish_interval_s: float = 300.0


@dataclass(frozen=True)
class DaemonConfig:
    socket: Path | str = DEFAULT_SOCKET_PATH
    socket_mode: int = 0o660
    log_level: str = "WARNING"
    csv: CsvConfig = field(default_factory=CsvConfig)
    mqtt: MqttConfig = field(default_factory=MqttConfig)


@dataclass(frozen=True)
class AppConfig:
    path: Path | None = None
    ble: BleConfig = field(default_factory=BleConfig)
    polling: PollingConfig = field(default_factory=PollingConfig)
    daemon: DaemonConfig = field(default_factory=DaemonConfig)


def resolve_config_path(explicit: Path | None = None) -> Path:
    if explicit is not None:
        return explicit.expanduser()
    return DEFAULT_CONFIG_PATH


def load_config(path: Path | None = None) -> AppConfig:
    resolved = resolve_config_path(path)
    if not resolved.is_file():
        raise ConfigError(f"Config file not found: {resolved}")
    try:
        raw = yaml.safe_load(resolved.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {resolved}: {exc}") from exc
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError(f"Config root must be a mapping: {resolved}")
    return parse_config(raw, resolved)


def parse_config(raw: dict[str, Any], path: Path | None = None) -> AppConfig:
    unknown = set(raw) - {"ble", "polling", "daemon"}
    if unknown:
        raise ConfigError(f"Unknown top-level key(s): {', '.join(sorted(unknown))}")
    ble = _parse_ble(_mapping(raw.get("ble"), "ble"))
    polling = _parse_polling(_mapping(raw.get("polling"), "polling"))
    daemon = _parse_daemon(_mapping(raw.get("daemon"), "daemon"))
    return AppConfig(path=path, ble=ble, polling=polling, daemon=daemon)


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{name} must be a mapping")
    return value


def _check_keys(raw: dict[str, Any], allowed: set[str], name: str) -> None:
    unknown = set(raw) - allowed
    if unknown:
        raise ConfigError(f"Unknown key(s) in {name}: {', '.join(sorted(unknown))}")


def _float(raw: dict[str, Any], key: str, default: float, name: str, *, minimum: float = 0.0, allow_zero: bool = False) -> float:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{name}.{key} must be a number")
    value = float(value)
    if value < minimum or (value == minimum and not allow_zero):
        raise ConfigError(f"{name}.{key} must be {'>=' if allow_zero else '>'} {minimum:g}")
    return value


def _int(raw: dict[str, Any], key: str, default: int, name: str, *, minimum: int = 1, maximum: int | None = None) -> int:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{name}.{key} must be an integer")
    if value < minimum or (maximum is not None and value > maximum):
        bound = f"between {minimum} and {maximum}" if maximum is not None else f">= {minimum}"
        raise ConfigError(f"{name}.{key} must be {bound}")
    return value


def _bool(raw: dict[str, Any], key: str, default: bool, name: str) -> bool:
    value = raw.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{name}.{key} must be true or false")
    return value


def _str(raw: dict[str, Any], key: str, default: str | None, name: str) -> str | None:
    value = raw.get(key, default)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{name}.{key} must be a non-empty string")
    return value


def _parse_ble(raw: dict[str, Any]) -> BleConfig:
    d = BleConfig()
    _check_keys(raw, {f.name for f in fields(BleConfig)}, "ble")
    address = _str(raw, "address", d.address, "ble")
    if address is not None:
        address = address.upper()
    return BleConfig(
        adapter=_str(raw, "adapter", d.adapter, "ble"),
        address=address,
        scan_duration_s=_float(raw, "scan_duration_s", d.scan_duration_s, "ble"),
        search_interval_s=_float(raw, "search_interval_s", d.search_interval_s, "ble"),
        rescan_interval_s=_float(raw, "rescan_interval_s", d.rescan_interval_s, "ble"),
        switch_hysteresis_db=_float(raw, "switch_hysteresis_db", d.switch_hysteresis_db, "ble", allow_zero=True),
        adv_max_age_s=_float(raw, "adv_max_age_s", d.adv_max_age_s, "ble"),
        connect_timeout_s=_float(raw, "connect_timeout_s", d.connect_timeout_s, "ble"),
        backoff_initial_s=_float(raw, "backoff_initial_s", d.backoff_initial_s, "ble"),
        backoff_max_s=_float(raw, "backoff_max_s", d.backoff_max_s, "ble"),
        inprogress_min_s=_float(raw, "inprogress_min_s", d.inprogress_min_s, "ble", allow_zero=True),
        stable_after_s=_float(raw, "stable_after_s", d.stable_after_s, "ble"),
        max_connect_failures=_int(raw, "max_connect_failures", d.max_connect_failures, "ble"),
        request_timeout_s=_float(raw, "request_timeout_s", d.request_timeout_s, "ble"),
        request_attempts=_int(raw, "request_attempts", d.request_attempts, "ble"),
    )


def _parse_polling(raw: dict[str, Any]) -> PollingConfig:
    d = PollingConfig()
    _check_keys(raw, {f.name for f in fields(PollingConfig)}, "polling")
    return PollingConfig(
        status_interval_s=_float(raw, "status_interval_s", d.status_interval_s, "polling"),
        parameters_interval_s=_float(raw, "parameters_interval_s", d.parameters_interval_s, "polling"),
        reconnect_after_failures=_int(raw, "reconnect_after_failures", d.reconnect_after_failures, "polling"),
        stale_after_factor=_float(raw, "stale_after_factor", d.stale_after_factor, "polling"),
    )


def _parse_socket(value: Any) -> Path | str:
    """A Unix socket path, or tcp://127.0.0.1:port (the default on Windows, which has no usable Unix sockets)."""
    text = str(value)
    if text.startswith("tcp://"):
        from solarlife.ipc import parse_endpoint

        parse_endpoint(text)  # raises ConfigError for a bad or non-loopback address
        return text
    return Path(text).expanduser()


def _parse_mode(value: Any) -> int:
    if isinstance(value, bool):
        raise ConfigError("daemon.socket_mode must be an octal mode")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.lower().removeprefix("0o")
        try:
            return int(text, 8)
        except ValueError:
            pass
    raise ConfigError("daemon.socket_mode must be an octal mode such as \"0660\"")


def _parse_daemon(raw: dict[str, Any]) -> DaemonConfig:
    d = DaemonConfig()
    _check_keys(raw, {"socket", "socket_mode", "log_level", "csv", "mqtt"}, "daemon")
    level = str(raw.get("log_level", d.log_level)).upper()
    if level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
        raise ConfigError("daemon.log_level must be DEBUG, INFO, WARNING or ERROR")
    csv_raw = _mapping(raw.get("csv"), "daemon.csv")
    _check_keys(csv_raw, {"enabled", "path"}, "daemon.csv")
    mqtt_raw = _mapping(raw.get("mqtt"), "daemon.mqtt")
    _check_keys(mqtt_raw, {f.name for f in fields(MqttConfig)}, "daemon.mqtt")
    md = MqttConfig()
    return DaemonConfig(
        socket=_parse_socket(_str(raw, "socket", str(d.socket), "daemon") or d.socket),
        socket_mode=_parse_mode(raw.get("socket_mode", d.socket_mode)),
        log_level=level,
        csv=CsvConfig(
            enabled=_bool(csv_raw, "enabled", True, "daemon.csv"),
            path=Path(_str(csv_raw, "path", str(CsvConfig().path), "daemon.csv") or CsvConfig().path).expanduser(),
        ),
        mqtt=MqttConfig(
            enabled=_bool(mqtt_raw, "enabled", md.enabled, "daemon.mqtt"),
            host=_str(mqtt_raw, "host", md.host, "daemon.mqtt") or md.host,
            port=_int(mqtt_raw, "port", md.port, "daemon.mqtt", minimum=1, maximum=65535),
            qos=_int(mqtt_raw, "qos", md.qos, "daemon.mqtt", minimum=0, maximum=2),
            csv=_bool(mqtt_raw, "csv", md.csv, "daemon.mqtt"),
            json=_bool(mqtt_raw, "json", md.json, "daemon.mqtt"),
            cbor=_bool(mqtt_raw, "cbor", md.cbor, "daemon.mqtt"),
            publish_interval_s=_float(mqtt_raw, "publish_interval_s", md.publish_interval_s, "daemon.mqtt", allow_zero=True),
        ),
    )
