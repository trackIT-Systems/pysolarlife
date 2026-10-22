import asyncio
import csv
import json
import os
import shutil
import socket
import sys
import tempfile
import time
from pathlib import Path

import cbor2
import pytest

from fakes import FakeBackend, FakeClock, FakeDevice, FakeMqtt, free_port
from solarlife import ipc
from solarlife.config import AppConfig, BleConfig, CsvConfig, DaemonConfig, MqttConfig, PollingConfig
from solarlife.daemon import Daemon
from solarlife.errors import DaemonError
from solarlife.outputs import Outputs
from solarlife.outputs.csv import CsvOutput
from solarlife.outputs.mqtt import MqttOutput

A = "A4:C1:38:6F:0E:2B"
B = "B4:C1:38:6F:0E:2C"


class Env:
    """A daemon wired to the fake backend, with real socket and real CSV/MQTT sinks."""

    def __init__(self, scan_results, *, tcp=False, **ble):
        # a short directory: macOS limits the length of a Unix socket path
        self.dir = Path(tempfile.mkdtemp(dir="/tmp" if os.path.isdir("/tmp") else None, prefix="sl-"))
        self.tcp = tcp or sys.platform.startswith("win")  # Windows has no usable Unix sockets
        self.sock = f"tcp://127.0.0.1:{free_port()}" if self.tcp else self.dir / "run" / "s.sock"
        self.csv_dir = self.dir / "data" / "tracker" / "solarlife"
        self.mqtt = FakeMqtt()
        self.backend = FakeBackend()
        self.backend.scan_results = scan_results
        ble_cfg = dict(
            scan_duration_s=0.01, search_interval_s=0.03, rescan_interval_s=3600, backoff_initial_s=0.01,
            backoff_max_s=0.03, inprogress_min_s=0.0, request_timeout_s=0.3, connect_timeout_s=1.0,
        )
        ble_cfg.update(ble)
        self.config = AppConfig(
            ble=BleConfig(**ble_cfg),
            polling=PollingConfig(status_interval_s=0.1, parameters_interval_s=3600),
            daemon=DaemonConfig(socket=self.sock, csv=CsvConfig(path=self.dir), mqtt=MqttConfig(publish_interval_s=0)),
        )
        clock = FakeClock(synced=True)
        outputs = Outputs([
            CsvOutput(self.dir / "data", hostname="tracker", clock=clock),
            MqttOutput(self.config.daemon.mqtt, hostname="tracker", client=self.mqtt, clock=clock),
        ])
        self.daemon = Daemon(self.config, backend=self.backend, outputs=outputs, hostname="tracker")
        self.task = None
        self.exit_code = None

    async def __aenter__(self):
        self.task = asyncio.create_task(self.daemon.run(handle_signals=False))
        await until(self._listening)
        return self

    def _listening(self) -> bool:
        if not self.tcp:
            return self.sock.exists()
        try:
            socket.create_connection((self.daemon.server.endpoint.host, self.daemon.server.endpoint.port), timeout=0.2).close()
            return True
        except OSError:
            return False

    async def __aexit__(self, *exc):
        await self.stop()
        shutil.rmtree(self.dir, ignore_errors=True)

    async def stop(self):
        self.daemon.stop_event.set()
        self.exit_code = await asyncio.wait_for(self.task, 5)

    async def call(self, cmd, **args):
        return await asyncio.to_thread(ipc.request, self.sock, cmd, args, timeout=5)

    async def wait_connected(self):
        await until(lambda: self.daemon.manager.session is not None and self.daemon.cache.blocks["parameters"].reading is not None)


async def until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.005)


def run(coro):
    return asyncio.run(coro)


def test_connects_polls_and_reports_to_csv_and_mqtt_then_stops_cleanly():
    async def go():
        async with Env([{A: -60}]) as env:
            await env.wait_connected()
            await until(lambda: len([t for t, _, _ in env.mqtt.published if t.endswith("a4c1386f0e2b/status/json")]) >= 3)

            status = await env.call("status")
            assert status["state"] == "connected" and status["device"]["address"] == A
            assert status["blocks"]["status"]["stale"] is False and status["connection"]["connects"] == 1

            cached = await env.call("cached")
            assert len(cached["blocks"]["status"]["values"]) == 44 and len(cached["blocks"]["parameters"]["values"]) == 12
            only = await env.call("cached", block="parameters")
            assert list(only["blocks"]) == ["parameters"]

            link = env.backend.links[0]
        assert env.exit_code == 0
        assert link.disconnect_calls == 1

    run(go())


def test_outputs_have_the_expected_content():
    async def go():
        env = Env([{A: -60}])
        async with env:
            await env.wait_connected()
            await until(lambda: len([t for t, _, _ in env.mqtt.published if t.endswith("a4c1386f0e2b/status/cbor")]) >= 2)
            files = {path.parent.name: path for path in env.csv_dir.glob("*/*.csv")}
            rows = {key: list(csv.DictReader(path.read_text(encoding="utf-8").splitlines(), delimiter=";")) for key, path in files.items()}
            await env.stop()
            published = list(env.mqtt.published)
            final_state = [(t, p, r) for t, p, r in published if t == "tracker/solarlife/state/json"][-1]
            assert json.loads(final_state[1])["state"] == "offline" and final_state[2] is True
            kinds = {json.loads(p)["event"] for t, p, _ in published if t == "tracker/solarlife/events/json"}
            assert {"daemon_started", "connected", "scan_completed", "daemon_stopped"} <= kinds
            reading_json = [json.loads(p) for t, p, _ in published if t == "tracker/solarlife/a4c1386f0e2b/status/json"]
            reading_cbor = [cbor2.loads(p) for t, p, _ in published if t == "tracker/solarlife/a4c1386f0e2b/status/cbor"]
            assert [list(r.values())[1:] for r in reading_json] == [r[1:] for r in reading_cbor]
            assert reading_json[0]["battery_voltage_level"] == 24.0 and reading_json[0]["device"] == A
            assert any(t == "tracker/solarlife/a4c1386f0e2b/parameters/csv" for t, _, _ in published)
            assert set(rows) == {"status", "parameters", "events"}
            assert any(r["event"] == "connected" for r in rows["events"])
            assert all(r["device"] == A and r["synced"] == "True" for r in rows["status"])
            states = [json.loads(p)["state"] for t, p, _ in published if t == "tracker/solarlife/state/json"]
            assert states[0] == "scanning" and "connected" in states and states[-1] == "offline"

    run(go())


def test_live_read_and_raw_range_read():
    async def go():
        async with Env([{A: -60}]) as env:
            await env.wait_connected()
            result = await env.call("read", block="status")
            assert result["device"] == A and len(result["blocks"]["status"]["values"]) == 44
            both = await env.call("read", block="all")
            assert set(both["blocks"]) == {"status", "parameters"}
            raw = await env.call("read", address="0x3032", count=1)
            assert raw["values"][0]["name"] == "battery_voltage_level" and raw["values"][0]["value"] == 24.0
            with pytest.raises(DaemonError) as err:
                await env.call("read", block="bogus")
            assert err.value.type == "bad_request"
            with pytest.raises(DaemonError) as err:
                await env.call("read", address="zero", count=1)
            assert err.value.type == "bad_request"

    run(go())


def test_write_validates_writes_and_reads_back():
    async def go():
        async with Env([{A: -60}]) as env:
            await env.wait_connected()
            device = env.backend.devices[A]
            result = await env.call("write", values={"float_voltage": "13.8", "boost_voltage": 14.4})
            assert device.registers[0x9026] == 1380 and device.registers[0x9024] == 1440
            assert {w["name"] for w in result["written"]} == {"float_voltage", "boost_voltage"}
            readback = {v["name"]: v["value"] for v in result["readback"]["parameters"]}
            assert readback["float_voltage"] == 13.8 and readback["boost_voltage"] == 14.4
            cached = {v["name"]: v["value"] for v in (await env.call("cached", block="parameters"))["blocks"]["parameters"]["values"]}
            assert cached["float_voltage"] == 13.8

            before = len(env.backend.links[0].writes)
            for values, kind in [({"clear_all": "Clear"}, "invalid_write"), ({"equipment_id": 1}, "invalid_write"), ({"battery_type": "plutonium"}, "invalid_write"), ({}, "bad_request")]:
                with pytest.raises(DaemonError) as err:
                    await env.call("write", values=values)
                assert err.value.type == kind
            assert len(env.backend.links[0].writes) == before  # nothing was sent

            await env.call("write", values={"clear_all": "Clear"}, force=True)

    run(go())


def test_reconnect_and_scan_commands():
    async def go():
        async with Env([{A: -60}]) as env:
            await env.wait_connected()
            assert await env.call("reconnect") == {"requested": True}
            await until(lambda: len(env.backend.links) == 2 and env.daemon.manager.session is not None)
            assert env.backend.links[0].disconnect_calls == 1
            scan = await env.call("rescan")
            assert [d["address"] for d in scan["devices"]] == [A] and scan["devices"][0]["current"] is True
            assert (await env.call("scan"))["devices"][0]["rssi"] == -60

    run(go())


def test_scan_lists_the_connected_device_even_though_it_does_not_advertise_anymore():
    async def go():
        async with Env([{A: -60}, {}], rescan_interval_s=3600) as env:
            await env.wait_connected()
            await env.call("rescan")  # the second scan sees nobody: a connected SolarLife is silent
            scan = await env.call("scan")
            assert scan["devices"] == [{"address": A, "name": "SolarLife", "rssi": -60, "samples": 0, "current": True, "advertising": False}]
            status = await env.call("status")
            assert status["state"] == "connected"
            assert status["scan"]["devices"] == 0

    run(go())


def test_since_in_the_status_is_the_connect_time_only_while_connected():
    async def go():
        async with Env([{A: -60}]) as env:
            await env.wait_connected()
            connected = (await env.call("status"))["since"]
            await asyncio.sleep(0.05)
            env.daemon.manager._set_state(env.daemon.manager.state.__class__.RESCANNING)
            rescanning = (await env.call("status"))["since"]
            assert rescanning > connected

    run(go())


def test_commands_report_not_connected_while_no_device_is_found():
    async def go():
        async with Env([{}]) as env:
            await until(lambda: env.daemon.manager.state.value == "scanning")
            for cmd, args in [("read", {"block": "status"}), ("write", {"values": {"float_voltage": 13.8}}), ("read", {"address": "0x3000"})]:
                with pytest.raises(DaemonError) as err:
                    await env.call(cmd, **args)
                assert err.value.type == "not_connected"
            status = await env.call("status")
            assert status["state"] == "scanning" and status["device"] is None
            cached = await env.call("cached")
            assert cached["connected"] is False and cached["blocks"]["status"]["values"] == []
            assert cached["blocks"]["status"]["stale"] is True

    run(go())


def test_the_daemon_survives_a_device_dropping_and_serves_cached_values_meanwhile():
    async def go():
        async with Env([{A: -60}]) as env:
            await env.wait_connected()
            env.backend.links[0].trigger_disconnect()
            await until(lambda: len(env.backend.links) == 2 and env.daemon.manager.session is not None)
            status = await env.call("status")
            assert status["connection"]["disconnects"] == 1 and status["connection"]["connects"] == 2

    run(go())


def test_switching_devices_clears_the_cache():
    async def go():
        env = Env([{A: -70}, {A: -70, B: -50}], switch_hysteresis_db=10)
        async with env:
            await env.wait_connected()
            first_device = env.daemon.manager.device.address
            await env.call("rescan")
            await until(lambda: env.daemon.manager.device and env.daemon.manager.device.address == B and env.daemon.manager.session is not None)
            assert first_device == A
            await until(lambda: env.daemon.cache.blocks["status"].reading is not None and env.daemon.cache.blocks["status"].reading.device == B)

    run(go())


def test_the_daemon_exits_with_an_error_if_a_core_task_dies():
    async def go():
        env = Env([{A: -60}])

        async def broken():
            raise RuntimeError("bug")

        env.daemon.manager.run = broken
        env.task = asyncio.create_task(env.daemon.run(handle_signals=False))
        code = await asyncio.wait_for(env.task, 5)
        assert code == 1
        shutil.rmtree(env.dir, ignore_errors=True)

    run(go())


def test_main_reports_startup_problems_without_a_traceback(tmp_path, caplog, monkeypatch):
    import logging

    from solarlife import daemon

    root = logging.getLogger()
    monkeypatch.setattr(root, "level", root.level)  # main() sets the configured level
    assert daemon.main(["--config", str(tmp_path / "missing.yml")]) == 1
    [record] = [r for r in caplog.records if r.name == "solarlife.daemon"]
    assert record.levelno == logging.CRITICAL and "not found" in record.getMessage() and record.exc_info is None
    caplog.clear()

    blocked = tmp_path / "file"
    blocked.write_text("x")  # a file where the csv directory should be created
    config = tmp_path / "c.yml"
    config.write_text(f"daemon:\n  socket: tcp://127.0.0.1:{free_port()}\n  csv: {{path: {blocked}}}\n  mqtt: {{enabled: false}}\n")
    monkeypatch.setattr("solarlife.ble.bleak_backend.BleakBackend", lambda adapter=None: FakeBackend())
    assert daemon.main(["--config", str(config)]) == 1
    [record] = [r for r in caplog.records if r.levelno == logging.CRITICAL]
    assert record.name == "solarlife.daemon" and record.getMessage().startswith("Cannot continue: ")
    assert record.exc_info is None


def test_profile_save_change_and_restore_through_the_daemon(tmp_path, capsys):
    from solarlife import cli

    async def go():
        async with Env([{A: -60}]) as env:
            await env.wait_connected()
            device = env.backend.devices[A]

            def cli_run(*argv):
                return asyncio.to_thread(cli.main, ["--socket", str(env.sock), "profile", *argv])

            before = tmp_path / "before.yml"
            assert await cli_run("save", str(before)) == 0
            original = dict(device.registers)

            assert await cli_run("load", "agm-12v", "--yes") == 0
            assert device.registers[0x9021] == 3 and device.registers[0x9026] == 1360  # AGM, float 13.6
            assert await cli_run("diff", "agm-12v") == 0
            assert "nothing to change" in capsys.readouterr().out.rsplit("written and verified", 1)[-1]

            assert await cli_run("load", str(before), "--yes") == 0
            assert device.registers == original

    run(go())
