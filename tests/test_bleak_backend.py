import asyncio
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("bleak")

from bleak.exc import BleakDBusError, BleakDeviceNotFoundError, BleakError  # noqa: E402

import solarlife.ble.bleak_backend as backend_module  # noqa: E402
from solarlife.ble import MANUFACTURER_ID, NOTIFY_UUID, WRITE_UUID, Advertisement  # noqa: E402
from solarlife.ble.bleak_backend import BleakBackend, BleakLink, map_connect_error, map_write_error  # noqa: E402
from solarlife.errors import ConnectError, LinkError, NotConnectedError  # noqa: E402


def run(coro):
    return asyncio.run(coro)


def adv(address="AA:BB:CC:DD:EE:FF", handle="handle"):
    return Advertisement(address, "SolarLife", -60, 3, time.monotonic(), handle)


class FakeScanner:
    instances = []
    script = []  # (device, advertisement) pairs delivered on start()
    stop_error = None

    def __init__(self, detection_callback=None, **kwargs):
        self.callback = detection_callback
        self.kwargs = kwargs
        self.started = self.stopped = False
        FakeScanner.instances.append(self)

    async def start(self):
        self.started = True
        for device, advertisement in FakeScanner.script:
            self.callback(device, advertisement)

    async def stop(self):
        self.stopped = True
        if FakeScanner.stop_error:
            raise FakeScanner.stop_error


def device(address, name=None):
    return SimpleNamespace(address=address, name=name)


def advertisement(rssi, manufacturer=True, local_name=None):
    return SimpleNamespace(rssi=rssi, manufacturer_data={MANUFACTURER_ID: b"\x10\x23"} if manufacturer else {0x004C: b"x"}, local_name=local_name)


@pytest.fixture
def scanner(monkeypatch):
    FakeScanner.instances, FakeScanner.script, FakeScanner.stop_error = [], [], None
    monkeypatch.setattr(backend_module, "BleakScanner", FakeScanner)
    return FakeScanner


def test_scan_filters_by_manufacturer_and_averages_rssi(scanner):
    a, b, other = device("A"), device("B", "dev-b"), device("C")
    scanner.script = [
        (a, advertisement(-60, local_name="SolarLife")),
        (a, advertisement(-70)),
        (a, advertisement(-65)),
        (b, advertisement(-50)),
        (other, advertisement(-30, manufacturer=False)),
    ]
    results = run(BleakBackend().scan(0.01))
    assert set(results) == {"A", "B"}
    assert results["A"].rssi == -65 and results["A"].samples == 3 and results["A"].name == "SolarLife"
    assert results["B"].rssi == -50 and results["B"].name == "dev-b"
    assert results["A"].handle is a
    assert scanner.instances[0].started and scanner.instances[0].stopped


def test_scan_ends_early_once_the_wanted_device_is_seen(scanner):
    scanner.script = [(device("A"), advertisement(-60)), (device("B"), advertisement(-50))]
    started = time.monotonic()
    results = run(BleakBackend().scan(30, until_address="A"))
    assert time.monotonic() - started < 1
    assert "A" in results and scanner.instances[0].stopped
    # a different wanted address is not seen, so the full duration is used
    started = time.monotonic()
    assert set(run(BleakBackend().scan(0.2, until_address="Z"))) == {"A", "B"}
    assert time.monotonic() - started >= 0.2


def test_scan_passes_the_adapter_and_survives_a_failing_stop(scanner):
    scanner.stop_error = RuntimeError("stop failed")
    assert run(BleakBackend("hci1").scan(0.01)) == {}
    assert scanner.instances[0].kwargs == {"adapter": "hci1"}
    scanner.stop_error = None
    run(BleakBackend().scan(0.01))
    assert scanner.instances[1].kwargs == {}


def test_scan_stops_the_scanner_when_cancelled(scanner):
    async def go():
        task = asyncio.create_task(BleakBackend().scan(30))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(go())
    assert scanner.instances[0].stopped


class FakeClient:
    instances = []
    connect_error = None
    connect_delay = 0.0

    def __init__(self, target, disconnected_callback=None, timeout=None, **kwargs):
        self.target = target
        self.disconnected_callback = disconnected_callback
        self.timeout = timeout
        self.kwargs = kwargs
        self.is_connected = False
        self.disconnects = 0
        self.notify = None
        self.written = []
        self.write_error = None
        FakeClient.instances.append(self)

    async def connect(self):
        await asyncio.sleep(FakeClient.connect_delay)
        if FakeClient.connect_error:
            raise FakeClient.connect_error
        self.is_connected = True

    async def disconnect(self):
        self.disconnects += 1
        self.is_connected = False

    async def start_notify(self, uuid, handler):
        assert uuid == NOTIFY_UUID
        self.notify = handler

    async def write_gatt_char(self, uuid, data):
        assert uuid == WRITE_UUID
        if self.write_error:
            raise self.write_error
        self.written.append(bytes(data))


@pytest.fixture
def client(monkeypatch):
    FakeClient.instances, FakeClient.connect_error, FakeClient.connect_delay = [], None, 0.0
    monkeypatch.setattr(backend_module, "BleakClient", FakeClient)
    return FakeClient


def test_connect_uses_the_scanned_device_and_returns_a_link(client):
    async def go():
        link = await BleakBackend("hci0").connect(adv(handle="the-ble-device"), timeout_s=5, on_disconnect=lambda: None)
        assert isinstance(link, BleakLink) and link.is_connected and link.address == "AA:BB:CC:DD:EE:FF"
        c = client.instances[0]
        assert c.target == "the-ble-device" and c.timeout == 5 and c.kwargs == {"adapter": "hci0"}
        # notifications arrive as bytes, writes go to the write characteristic
        got = []
        await link.start_notify(got.append)
        c.notify(None, bytearray(b"\x01\x02"))
        assert got == [b"\x01\x02"]
        await link.write(b"\xaa")
        assert c.written == [b"\xaa"]
        await link.disconnect()
        assert not link.is_connected

    run(go())


def test_connect_without_a_handle_falls_back_to_the_address(client):
    run(BleakBackend().connect(adv(handle=None), timeout_s=5, on_disconnect=lambda: None))
    assert client.instances[0].target == "AA:BB:CC:DD:EE:FF"


def test_disconnect_callback_is_marshalled_to_the_loop(client):
    async def go():
        called = []
        await BleakBackend().connect(adv(), timeout_s=5, on_disconnect=lambda: called.append(1))
        client.instances[0].disconnected_callback(client.instances[0])
        assert called == []  # scheduled, not run inline
        await asyncio.sleep(0)
        assert called == [1]

    run(go())


@pytest.mark.parametrize(
    "error,flags",
    [
        (BleakDBusError("org.bluez.Error.InProgress", []), {"in_progress": True}),
        (BleakDBusError("org.bluez.Error.Failed", ["le-connection-abort-by-local"]), {}),
        (BleakDeviceNotFoundError("AA:BB"), {"not_found": True}),
        (BleakError("boom"), {}),
        (TimeoutError(), {}),
        (EOFError(), {}),
    ],
)
def test_connect_errors_are_mapped_and_the_half_open_connect_is_cleaned_up(client, error, flags):
    client.connect_error = error

    async def go():
        with pytest.raises(ConnectError) as err:
            await BleakBackend().connect(adv(), timeout_s=5, on_disconnect=lambda: None)
        assert err.value.in_progress == flags.get("in_progress", False)
        assert err.value.not_found == flags.get("not_found", False)
        assert client.instances[0].disconnects == 1

    run(go())


def test_connect_times_out_even_if_bleak_hangs(client, monkeypatch):
    monkeypatch.setattr(backend_module, "CONNECT_GRACE_S", 0.05)
    client.connect_delay = 30

    async def go():
        with pytest.raises(ConnectError, match="timed out"):
            await BleakBackend().connect(adv(), timeout_s=0.05, on_disconnect=lambda: None)
        assert client.instances[0].disconnects == 1

    run(go())


def test_a_cancelled_connect_is_cleaned_up(client):
    client.connect_delay = 30

    async def go():
        task = asyncio.create_task(BleakBackend().connect(adv(), timeout_s=60, on_disconnect=lambda: None))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert client.instances[0].disconnects == 1

    run(go())


def test_write_errors_are_mapped(client):
    async def go():
        link = await BleakBackend().connect(adv(), timeout_s=5, on_disconnect=lambda: None)
        c = client.instances[0]
        c.write_error = BleakDBusError("org.bluez.Error.InProgress", [])
        with pytest.raises(LinkError):
            await link.write(b"x")
        c.write_error = BleakDBusError("org.bluez.Error.NotConnected", [])
        with pytest.raises(NotConnectedError):
            await link.write(b"x")
        c.write_error = BleakError("Characteristic write failed")
        c.is_connected = False
        with pytest.raises(NotConnectedError):
            await link.write(b"x")

    run(go())


def test_start_notify_failure_is_a_link_error(client):
    async def go():
        link = await BleakBackend().connect(adv(), timeout_s=5, on_disconnect=lambda: None)

        async def broken(uuid, handler):
            raise BleakError("characteristic not found")

        client.instances[0].start_notify = broken
        with pytest.raises(LinkError, match="notifications"):
            await link.start_notify(lambda data: None)

    run(go())


def test_disconnect_tolerates_eof(client):
    async def go():
        link = await BleakBackend().connect(adv(), timeout_s=5, on_disconnect=lambda: None)

        async def eof():
            raise EOFError

        client.instances[0].disconnect = eof
        await link.disconnect()

    run(go())


def test_error_mapping_helpers():
    assert map_connect_error(BleakDBusError("org.bluez.Error.InProgress", [])).in_progress
    assert map_connect_error(RuntimeError("x")).in_progress is False
    assert isinstance(map_write_error(BleakError("x"), connected=True), LinkError)
    assert isinstance(map_write_error(BleakError("Not connected"), connected=True), NotConnectedError)
