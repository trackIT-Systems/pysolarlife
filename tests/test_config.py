from pathlib import Path

import pytest

from dataclasses import replace

from solarlife.config import DEFAULT_SOCKET_PATH, AppConfig, load_config, parse_config
from solarlife.errors import ConfigError

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "solarlife.yml"


def test_example_config_loads_and_matches_defaults():
    cfg = load_config(EXAMPLE)
    default = AppConfig()
    assert cfg.ble == default.ble
    assert cfg.polling == default.polling
    # the example shows the Linux (tsOS) paths; the defaults of other systems differ
    assert cfg.daemon == replace(default.daemon, socket=Path("/run/solarlife/solarlife.sock"), csv=replace(default.daemon.csv, path=Path("/data")))


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


def test_default_paths_per_platform(tmp_path):
    from solarlife.platform import default_paths

    linux = default_paths("linux")
    assert linux.config == Path("/boot/firmware/solarlife.yml") and linux.socket == Path("/run/solarlife/solarlife.sock") and linux.data_dir == Path("/data")
    mac = default_paths("darwin", home=tmp_path)
    assert mac.state_dir == tmp_path / "Library" / "Application Support" / "solarlife" and mac.socket == mac.state_dir / "solarlife.sock" and mac.data_dir == mac.state_dir / "data"
    win = default_paths("win32", env={"PROGRAMDATA": "C:/ProgramData"})
    assert win.state_dir == Path("C:/ProgramData/solarlife") and win.socket == "tcp://127.0.0.1:47821" and win.config.name == "solarlife.yml"
    assert default_paths("win32", env={}, home=tmp_path).state_dir == tmp_path / "AppData" / "Local" / "solarlife"
    assert DEFAULT_SOCKET_PATH == default_paths().socket


def test_the_control_socket_can_be_a_loopback_tcp_endpoint():
    assert parse_config({"daemon": {"socket": "tcp://127.0.0.1:5000"}}).daemon.socket == "tcp://127.0.0.1:5000"
    for bad, message in (("tcp://0.0.0.0:5000", "loopback"), ("tcp://[::1]:5000", "tcp://127.0.0.1:port"), ("tcp://example.org:5000", "tcp://127.0.0.1:port"), ("tcp://127.0.0.1:0", "port"), ("tcp://127.0.0.1", "tcp://127.0.0.1:port")):
        with pytest.raises(ConfigError, match=message):
            parse_config({"daemon": {"socket": bad}})


def test_paths_in_the_config_expand_the_home_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    config = parse_config({"daemon": {"socket": "~/s.sock", "csv": {"path": "~/data"}}})
    assert config.daemon.socket == tmp_path / "s.sock" and config.daemon.csv.path == tmp_path / "data"


def test_the_macos_example_config_is_valid_and_has_the_macos_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    config = load_config(Path(__file__).resolve().parents[1] / "config" / "solarlife.macos.yml")
    base = tmp_path / "Library" / "Application Support" / "solarlife"
    assert config.daemon.socket == base / "solarlife.sock" and config.daemon.csv.path == base / "data"
    assert not config.daemon.mqtt.enabled and config.daemon.socket_mode == 0o600


def test_the_changelog_has_a_section_for_the_version():
    from solarlife import __version__

    text = (Path(__file__).resolve().parents[1] / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"## [{__version__}]" in text and f"[{__version__}]: https://github.com/trackIT-Systems/pysolarlife/releases/tag/v{__version__}" in text
