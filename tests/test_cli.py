import asyncio
import json
import os
import shutil
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from fakes import free_port
from solarlife import cli
from solarlife.errors import DeviceException, NotConnectedError
from solarlife.server import ControlServer

NOW = datetime(2026, 10, 13, 12, 0, 0, tzinfo=timezone.utc)
VALUE = {"name": "battery_voltage_level", "friendly": "Current battery voltage level", "unit": "V", "address": "0x3032", "value": 24.0}
STATUS = {
    "version": "1.0.0",
    "state": "connected",
    "since": (NOW - timedelta(hours=2, minutes=3)).isoformat().replace("+00:00", "Z"),
    "uptime_s": 7500,
    "device": {"address": "A4:C1:38:6F:0E:2B", "name": "SolarLife", "rssi": -67, "pinned": False, "connected": True},
    "blocks": {
        "status": {"interval_s": 60, "last_ok": None, "age_s": 23.4, "stale": False, "last_error": None, "consecutive_failures": 0},
        "parameters": {"interval_s": 3600, "last_ok": None, "age_s": 2460.0, "stale": False, "last_error": None, "consecutive_failures": 0},
    },
    "connection": {"connect_attempts": 5, "connects": 3, "disconnects": 2, "last_error": "InProgress", "backoff_s": None},
    "scan": {"last": (NOW - timedelta(minutes=12)).isoformat().replace("+00:00", "Z"), "devices": 2, "next_in_s": 2880},
    "unsolicited": 0,
}


# -- formatting ---------------------------------------------------------------


def test_fmt_duration():
    assert [cli.fmt_duration(x) for x in (None, 5, 59.9, 60, 2460, 7380, 93600)] == ["-", "5s", "59s", "1m", "41m", "2h03m", "1d02h"]


def test_format_status():
    text = cli.format_status(STATUS, NOW)
    lines = {l.split("  ", 1)[0].strip(): l.split("  ", 1)[1].strip() for l in text.splitlines()}
    assert lines["state"].startswith("connected since ") and lines["state"].endswith("(2h03m)")
    assert lines["device"] == "A4:C1:38:6F:0E:2B  SolarLife  rssi -67 dBm (at connect)"
    assert lines["status"] == "ok 23s ago   (every 1m)"
    assert lines["parameters"] == "ok 41m ago   (every 1h00m)"
    assert lines["connection"] == "5 attempts, 3 connects, 2 disconnects, last error: InProgress"
    assert lines["scan"] == "12m ago, 2 device(s), next in 48m"


def test_format_status_without_data_or_device():
    status = json.loads(json.dumps(STATUS))
    status["device"] = None
    status["state"] = "scanning"
    status["since"] = None
    status["blocks"]["status"].update(age_s=None, stale=True, last_error="no complete response")
    status["scan"] = {"last": None, "devices": 0, "next_in_s": None}
    status["connection"]["backoff_s"] = 4.0
    text = cli.format_status(status, NOW)
    assert "device      none" in text and "no data yet   (every 1m)   last error: no complete response" in text
    assert "scan        never" in text and "retrying in 4s" in text


def test_format_values_and_blocks():
    assert cli.format_values([VALUE, {**VALUE, "name": "battery_type", "unit": "", "value": "Lithium"}]).splitlines() == [
        "NAME                   VALUE    UNIT",
        "battery_voltage_level  24       V",
        "battery_type           Lithium",
    ]
    cached = {"device": "AA", "blocks": {"status": {"time": "x", "age_s": 61, "stale": True, "values": [VALUE]}, "parameters": {"time": None, "age_s": None, "stale": True, "values": []}}}
    text = cli.format_blocks(cached, live=False)
    assert "status (AA, 1m old, stale)" in text and "parameters: no data yet" in text
    assert "status (live, AA)" in cli.format_blocks({"device": "AA", "blocks": {"status": {"values": [VALUE]}}}, live=True)


def test_parse_assignments():
    assert cli.parse_assignments(["a=1", " b = x=y "]) == {"a": "1", "b": "x=y"}
    for bad in (["a"], ["=1"]):
        with pytest.raises(Exception, match="NAME=VALUE"):
            cli.parse_assignments(bad)


# -- commands against a stub daemon ----------------------------------------------


class StubDaemon:
    """A real ControlServer with canned handlers, in a background thread."""

    def __init__(self, handlers):
        self.dir = Path(tempfile.mkdtemp(dir="/tmp" if os.path.isdir("/tmp") else None, prefix="sl-"))
        self.sock = f"tcp://127.0.0.1:{free_port()}" if sys.platform.startswith("win") else self.dir / "s.sock"
        self.calls = []
        self.handlers = {name: self._wrap(name, h) for name, h in handlers.items()}
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _wrap(self, name, handler):
        async def wrapped(args):
            self.calls.append((name, args))
            if isinstance(handler, Exception):
                raise handler
            return handler

        return wrapped

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.server = ControlServer(self.sock, 0o600, self.handlers)
        self.loop.run_until_complete(self.server.start())
        self.ready.set()
        self.loop.run_forever()

    def __enter__(self):
        self.thread.start()
        assert self.ready.wait(5)
        return self

    def __exit__(self, *exc):
        asyncio.run_coroutine_threadsafe(self.server.stop(), self.loop).result(5)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)
        self.loop.close()
        shutil.rmtree(self.dir, ignore_errors=True)


def run_cli(daemon, *argv):
    return cli.main(["--socket", str(daemon.sock), *argv])


def test_status_command(capsys):
    with StubDaemon({"status": STATUS}) as d:
        assert run_cli(d, "status") == 0
    out = capsys.readouterr().out
    assert "A4:C1:38:6F:0E:2B" in out and "solarlifed 1.0.0" in out


def test_json_flag_works_before_and_after_the_command(capsys):
    with StubDaemon({"status": STATUS}) as d:
        assert cli.main(["status", "--json", "--socket", str(d.sock)]) == 0
        after = json.loads(capsys.readouterr().out)
        assert run_cli(d, "--json", "status") == 0
        before = json.loads(capsys.readouterr().out)
    assert after == before == STATUS


def test_cached_and_read_commands(capsys):
    cached = {"device": "AA", "connected": True, "blocks": {"parameters": {"time": "t", "age_s": 5, "stale": False, "values": [VALUE]}}}
    live = {"device": "AA", "blocks": {"status": {"time": "t", "values": [VALUE]}}}
    raw = {"device": "AA", "range": {"address": "0x3032", "count": 2}, "values": [VALUE]}
    with StubDaemon({"cached": cached, "read": live}) as d:
        assert run_cli(d, "cached", "parameters") == 0
        assert "parameters (AA, 5s old)" in capsys.readouterr().out
        assert d.calls[-1] == ("cached", {"block": "parameters"})
        assert run_cli(d, "read", "status") == 0
        assert "status (live, AA)" in capsys.readouterr().out
        assert d.calls[-1] == ("read", {"block": "status"})
    with StubDaemon({"read": raw}) as d:
        assert run_cli(d, "read", "--range", "0x3032:2") == 0
        assert d.calls[-1] == ("read", {"address": "0x3032", "count": 2})
        assert "battery_voltage_level" in capsys.readouterr().out
        assert run_cli(d, "read", "--range", "zzz") == 2
        assert "ADDRESS[:COUNT]" in capsys.readouterr().err


def test_scan_output_marks_a_connected_device_that_does_not_advertise(capsys):
    scan = {"time": "t", "devices": [{"address": "AA", "name": "SolarLife", "rssi": -62, "samples": 0, "current": True, "advertising": False}]}
    assert "-62 dBm (when connected)" in cli.format_scan(scan)


def test_reconnect_rescan_scan(capsys):
    scan = {"time": "t", "devices": [{"address": "AA", "name": "SolarLife", "rssi": -60, "samples": 9, "current": True}, {"address": "BB", "name": None, "rssi": -80, "samples": 2, "current": False}]}
    with StubDaemon({"reconnect": {"requested": True}, "rescan": scan, "scan": {"time": None, "devices": []}}) as d:
        assert run_cli(d, "reconnect") == 0 and "reconnect requested" in capsys.readouterr().out
        assert run_cli(d, "rescan") == 0
        out = capsys.readouterr().out
        assert "AA" in out and "-60 dBm" in out and "*" in out
        assert run_cli(d, "scan") == 0 and "no SolarLife devices found (no scan yet)" in capsys.readouterr().out


def test_write_with_yes_sends_the_request_and_shows_the_readback(capsys):
    result = {"written": [{"name": "float_voltage", "value": 13.6}], "readback": {"parameters": [{"name": "float_voltage", "value": 13.6, "unit": "V"}, {"name": "boost_voltage", "value": 14.4, "unit": "V"}]}}
    with StubDaemon({"write": result}) as d:
        assert run_cli(d, "write", "float_voltage=13.6", "--yes") == 0
        assert d.calls[-1] == ("write", {"values": {"float_voltage": "13.6"}, "force": False})
        out = capsys.readouterr().out
        assert "float_voltage" in out and "boost_voltage" not in out  # only what was written


def test_write_asks_for_confirmation(capsys, monkeypatch):
    result = {"written": [{"name": "float_voltage", "value": 13.6}], "readback": {}}
    with StubDaemon({"write": result}) as d:
        # not a terminal and no --yes: refuse
        monkeypatch.setattr("sys.stdin", type("S", (), {"isatty": lambda self: False})())
        assert run_cli(d, "write", "float_voltage=13.6") == 2
        assert d.calls == []
        # a terminal: ask
        monkeypatch.setattr("sys.stdin", type("S", (), {"isatty": lambda self: True})())
        monkeypatch.setattr("builtins.input", lambda prompt: "n")
        assert run_cli(d, "write", "float_voltage=13.6") == 1 and d.calls == []
        monkeypatch.setattr("builtins.input", lambda prompt: "y")
        assert run_cli(d, "write", "float_voltage=13.6") == 0 and len(d.calls) == 1
    assert "float_voltage = 13.6 V" in capsys.readouterr().out


def test_write_is_validated_before_contacting_the_daemon(capsys):
    with StubDaemon({"write": {"written": [], "readback": {}}}) as d:
        assert run_cli(d, "write", "equipment_id=1", "--yes") == 2
        assert "read-only" in capsys.readouterr().err
        assert run_cli(d, "write", "clear_all=Clear", "--yes") == 2
        assert "needs force" in capsys.readouterr().err
        assert run_cli(d, "write", "oops", "--yes") == 2
        assert d.calls == []
        assert run_cli(d, "write", "clear_all=Clear", "--yes", "--force") == 0
        assert d.calls[-1][1]["force"] is True


def test_daemon_errors_give_exit_code_1(capsys):
    with StubDaemon({"read": NotConnectedError("no device connected (state=scanning)"), "status": DeviceException(2)}) as d:
        assert run_cli(d, "read", "status") == 1
        assert "no device connected (state=scanning)" in capsys.readouterr().err
        assert run_cli(d, "status") == 1


def test_unreachable_daemon_gives_exit_code_3(capsys, tmp_path):
    assert cli.main(["--socket", f"tcp://127.0.0.1:{free_port()}", "status"]) == 3
    assert "is solarlifed running" in capsys.readouterr().err
    if sys.platform.startswith("win"):
        return  # no Unix sockets
    # a path that is too long for a unix socket is also just "unreachable", not a traceback
    assert cli.main(["--socket", str(tmp_path / ("x" * 120) / "none.sock"), "status"]) == 3
    assert "cannot connect" in capsys.readouterr().err


def test_variables_works_offline(capsys, tmp_path):
    assert cli.main(["--socket", str(tmp_path / "none.sock"), "variables", "--writable", "--block", "parameters"]) == 0
    out = capsys.readouterr().out
    assert "float_voltage" in out and "'Lithium'" in out and "equipment_id" not in out
    assert cli.main(["variables", "--json", "--block", "status"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 44 and all(r["block"] == "status" for r in rows)


def test_socket_comes_from_an_explicit_config(tmp_path, capsys):
    config = tmp_path / "c.yml"
    config.write_text("daemon:\n  socket: /tmp/from-config.sock\n")
    args = cli.build_parser().parse_args(["--config", str(config), "status"])
    assert cli.resolve_socket(args) == Path("/tmp/from-config.sock")
    config.write_text("daemon:\n  socket: tcp://127.0.0.1:5000\n")
    assert cli.resolve_socket(cli.build_parser().parse_args(["--config", str(config), "status"])) == "tcp://127.0.0.1:5000"
    config.write_text("daemon:\n  socket: /tmp/from-config.sock\n")
    args = cli.build_parser().parse_args(["--config", str(tmp_path / "missing.yml"), "status"])
    with pytest.raises(Exception, match="not found"):
        cli.resolve_socket(args)
    assert cli.main(["--config", str(tmp_path / "missing.yml"), "status"]) == 2


def test_help_and_usage_errors(capsys):
    with pytest.raises(SystemExit) as exit_:
        cli.main(["--help"])
    assert exit_.value.code == 0
    out = capsys.readouterr().out
    for command in ("status", "cached", "read", "write", "reconnect", "rescan", "scan", "variables"):
        assert command in out
    with pytest.raises(SystemExit) as exit_:
        cli.main([])
    assert exit_.value.code == 2
    with pytest.raises(SystemExit) as exit_:
        cli.main(["read", "bogus"])
    assert exit_.value.code == 2


# -- profiles ---------------------------------------------------------------------

PARAMS = [
    {"name": "battery_type", "value": "Lithium", "unit": ""},
    {"name": "float_voltage", "value": 13.6, "unit": "V"},
    {"name": "boost_voltage", "value": 14.4, "unit": "V"},
]
LIVE_PARAMS = {"device": "AA", "blocks": {"parameters": {"time": "t", "values": PARAMS}}}


def write_profile(path, **values):
    import yaml

    path.write_text(yaml.safe_dump({"name": "t", "values": values}))
    return str(path)


def test_profile_list_and_show_work_offline(capsys, tmp_path):
    assert cli.main(["--socket", str(tmp_path / "none.sock"), "profile", "list"]) == 0
    assert "agm-12v" in capsys.readouterr().out
    assert cli.main(["profile", "show", "agm-12v", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["values"]["battery_type"] == "AGM"
    assert cli.main(["profile", "show", "agm-12v"]) == 0
    assert "float_voltage: 13.6" in capsys.readouterr().out
    assert cli.main(["profile", "show", "nope"]) == 2
    assert "no premade profile" in capsys.readouterr().err


def test_profile_save(capsys, tmp_path):
    out = tmp_path / "p.yml"
    with StubDaemon({"read": LIVE_PARAMS}) as d:
        assert run_cli(d, "profile", "save", str(out), "--description", "mine") == 0
        assert d.calls[-1] == ("read", {"block": "parameters"})
        assert "saved 3 parameters" in capsys.readouterr().out
        text = out.read_text()
        assert "# from device AA" in text and "float_voltage: 13.6" in text and "description: mine" in text
        assert run_cli(d, "profile", "save", str(out)) == 2  # exists
        assert "--force" in capsys.readouterr().err
        assert run_cli(d, "profile", "save", str(out), "--force", "--name", "other") == 0
        assert "name: other" in out.read_text()
        assert run_cli(d, "profile", "save", "-") == 0
        assert "values:" in capsys.readouterr().out.split("saved", 1)[-1]


def test_profile_save_cached_needs_fresh_data(capsys, tmp_path):
    fresh = {"device": "AA", "blocks": {"parameters": {"time": "t", "age_s": 5, "stale": False, "values": PARAMS}}}
    stale = {"device": "AA", "blocks": {"parameters": {"time": "t", "age_s": 99999, "stale": True, "values": PARAMS}}}
    with StubDaemon({"cached": fresh}) as d:
        assert run_cli(d, "profile", "save", str(tmp_path / "a.yml"), "--cached") == 0
        assert d.calls[-1] == ("cached", {"block": "parameters"})
    with StubDaemon({"cached": stale}) as d:
        assert run_cli(d, "profile", "save", str(tmp_path / "b.yml"), "--cached") == 1
        assert not (tmp_path / "b.yml").exists() and "no recent cached parameters" in capsys.readouterr().err


def test_profile_diff_and_dry_run_write_nothing(capsys, tmp_path):
    path = write_profile(tmp_path / "p.yml", float_voltage=13.8, boost_voltage=14.4)
    with StubDaemon({"read": LIVE_PARAMS, "write": {"written": [], "readback": {}}}) as d:
        assert run_cli(d, "profile", "diff", path) == 0
        out = capsys.readouterr().out
        assert "1 of 2 value(s) differ" in out and "float_voltage" in out and "13.6  ->  13.8" in out and "boost_voltage" not in out
        assert run_cli(d, "profile", "load", path, "--dry-run") == 0
        assert [c[0] for c in d.calls] == ["read", "read"]


def test_profile_load_with_nothing_to_change(capsys, tmp_path):
    path = write_profile(tmp_path / "p.yml", float_voltage=13.6, battery_type="lithium")
    with StubDaemon({"read": LIVE_PARAMS}) as d:
        assert run_cli(d, "profile", "load", path, "--yes") == 0
        assert "nothing to change" in capsys.readouterr().out
        assert [c[0] for c in d.calls] == ["read"]


def test_profile_load_writes_only_the_differences_and_verifies(capsys, tmp_path):
    path = write_profile(tmp_path / "p.yml", float_voltage=13.8, boost_voltage=14.4, battery_type="AGM")
    readback = {"parameters": [{"name": "battery_type", "value": "AGM"}, {"name": "float_voltage", "value": 13.8}, {"name": "boost_voltage", "value": 14.4}]}
    with StubDaemon({"read": LIVE_PARAMS, "write": {"written": [], "readback": readback}}) as d:
        assert run_cli(d, "profile", "load", path, "--yes") == 0
        assert d.calls[-1] == ("write", {"values": {"battery_type": "AGM", "float_voltage": 13.8}, "force": False})
        assert "written and verified: 2 value(s)" in capsys.readouterr().out


def test_profile_load_reports_a_mismatch(capsys, tmp_path):
    path = write_profile(tmp_path / "p.yml", float_voltage=13.8)
    readback = {"parameters": [{"name": "float_voltage", "value": 13.6}]}
    with StubDaemon({"read": LIVE_PARAMS, "write": {"written": [], "readback": readback}}) as d:
        assert run_cli(d, "profile", "load", path, "--yes") == 1
    assert "float_voltage (wanted 13.8, got 13.6)" in capsys.readouterr().err
    with StubDaemon({"read": LIVE_PARAMS, "write": {"written": [], "readback": {}, "readback_errors": {"parameters": "timeout"}}}) as d:
        assert run_cli(d, "profile", "load", path, "--yes") == 1
    err = capsys.readouterr().err
    assert "could not read the parameters back: timeout" in err and "got nothing" in err


def test_profile_load_asks_for_confirmation(capsys, tmp_path, monkeypatch):
    path = write_profile(tmp_path / "p.yml", float_voltage=13.8)
    readback = {"parameters": [{"name": "float_voltage", "value": 13.8}]}
    with StubDaemon({"read": LIVE_PARAMS, "write": {"written": [], "readback": readback}}) as d:
        monkeypatch.setattr("sys.stdin", type("S", (), {"isatty": lambda self: False})())
        assert run_cli(d, "profile", "load", path) == 2
        monkeypatch.setattr("sys.stdin", type("S", (), {"isatty": lambda self: True})())
        monkeypatch.setattr("builtins.input", lambda prompt: "n")
        assert run_cli(d, "profile", "load", path) == 1
        assert "write" not in [c[0] for c in d.calls]
        monkeypatch.setattr("builtins.input", lambda prompt: "y")
        assert run_cli(d, "profile", "load", path) == 0
    assert "float_voltage = 13.8 V" in capsys.readouterr().out


def test_profile_load_validates_the_file_before_contacting_the_daemon(capsys, tmp_path):
    with StubDaemon({"read": LIVE_PARAMS}) as d:
        assert run_cli(d, "profile", "load", write_profile(tmp_path / "a.yml", slave_id=3), "--yes") == 2
        assert run_cli(d, "profile", "load", write_profile(tmp_path / "b.yml", battery_type="plutonium"), "--yes") == 2
        assert d.calls == []
    assert "not a profile parameter" in capsys.readouterr().err
