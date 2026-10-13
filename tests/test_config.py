from pathlib import Path

import pytest

from solarlife.config import AppConfig, load_config, parse_config
from solarlife.errors import ConfigError

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "solarlife.yml"


def test_example_config_loads_and_matches_defaults():
    cfg = load_config(EXAMPLE)
    default = AppConfig()
    assert cfg.ble == default.ble
    assert cfg.polling == default.polling
    assert cfg.daemon == default.daemon


def test_empty_config_uses_defaults():
    assert parse_config({}).ble.scan_duration_s == 10.0


def test_overrides():
    cfg = parse_config(
        {
            "ble": {"address": "10:23:81:1e:97:4c", "rescan_interval_s": 600},
            "polling": {"status_interval_s": 30},
            "daemon": {"socket_mode": "0600", "csv": {"enabled": False}, "mqtt": {"cbor": False, "qos": 1}},
        }
    )
    assert cfg.ble.address == "10:23:81:1E:97:4C"
    assert cfg.ble.rescan_interval_s == 600
    assert cfg.polling.status_interval_s == 30
    assert cfg.daemon.socket_mode == 0o600
    assert cfg.daemon.csv.enabled is False
    assert cfg.daemon.mqtt.cbor is False and cfg.daemon.mqtt.json is True
    assert cfg.daemon.mqtt.qos == 1


@pytest.mark.parametrize(
    "raw",
    [
        {"bogus": {}},
        {"ble": {"bogus": 1}},
        {"ble": {"scan_duration_s": 0}},
        {"ble": {"scan_duration_s": "ten"}},
        {"ble": {"request_attempts": 0}},
        {"ble": []},
        {"polling": {"status_interval_s": -1}},
        {"daemon": {"log_level": "LOUD"}},
        {"daemon": {"socket_mode": "rw"}},
        {"daemon": {"mqtt": {"port": 70000}}},
        {"daemon": {"mqtt": {"qos": 3}}},
        {"daemon": {"csv": {"enabled": "yes"}}},
    ],
)
def test_invalid_config_is_rejected(raw):
    with pytest.raises(ConfigError):
        parse_config(raw)


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yml")


def test_invalid_yaml(tmp_path):
    p = tmp_path / "c.yml"
    p.write_text("ble: [")
    with pytest.raises(ConfigError, match="Invalid YAML"):
        load_config(p)
