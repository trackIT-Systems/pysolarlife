import asyncio
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path

import pytest

from solarlife import ipc
from solarlife.errors import (
    BusyError,
    DaemonError,
    DaemonUnreachable,
    DeviceException,
    InvalidWriteError,
    NotConnectedError,
    TransactionTimeout,
)
from solarlife.server import BadRequest, ControlServer, error_type


@pytest.fixture
def sock_path():
    # unix socket paths are limited to ~100 characters (104 on macOS), pytest's tmp_path is too long
    if sys.platform.startswith("win"):
        pytest.skip("no Unix sockets")
    directory = tempfile.mkdtemp(dir="/tmp", prefix="sl-")
    yield Path(directory) / "sub" / "s.sock"
    shutil.rmtree(directory, ignore_errors=True)


def run(coro):
    return asyncio.run(coro)


async def call(path, cmd, args=None, timeout=5.0):
    return await asyncio.to_thread(ipc.request, path, cmd, args, timeout=timeout)


def make_handlers():
    async def echo(args):
        return {"echo": args}

    async def slow(args):
        await asyncio.sleep(0.3)
        return "done"

    def raiser(exc):
        async def handler(args):
            raise exc

        return handler

    return {
        "echo": echo,
        "slow": slow,
        "not_connected": raiser(NotConnectedError("no device")),
        "busy": raiser(BusyError("busy")),
        "timeout": raiser(TransactionTimeout("no answer")),
        "device": raiser(DeviceException(2)),
        "invalid": raiser(InvalidWriteError("bad value")),
        "bad": raiser(BadRequest("bad args")),
        "crash": raiser(RuntimeError("boom")),
    }


def test_socket_is_created_with_mode_and_removed_on_stop(sock_path):
    async def go():
        server = ControlServer(sock_path, 0o660, make_handlers())
        await server.start()
        assert stat.S_ISSOCK(sock_path.stat().st_mode)
        assert stat.S_IMODE(sock_path.stat().st_mode) == 0o660
        await server.stop()
        assert not sock_path.exists()

    run(go())


def test_a_stale_socket_file_is_replaced(sock_path):
    async def go():
        import socket

        sock_path.parent.mkdir(parents=True)
        stale = socket.socket(socket.AF_UNIX)
        stale.bind(str(sock_path))
        stale.close()
        assert sock_path.exists()
        server = ControlServer(sock_path, 0o660, make_handlers())
        await server.start()
        assert await call(sock_path, "echo", {"a": 1}) == {"echo": {"a": 1}}
        await server.stop()

    run(go())


def test_request_and_response(sock_path):
    async def go():
        server = ControlServer(sock_path, 0o660, make_handlers())
        await server.start()
        try:
            assert await call(sock_path, "echo", {"x": [1, 2]}) == {"echo": {"x": [1, 2]}}
            assert await call(sock_path, "echo") == {"echo": {}}
        finally:
            await server.stop()

    run(go())


@pytest.mark.parametrize(
    "cmd,kind",
    [
        ("not_connected", "not_connected"),
        ("busy", "busy"),
        ("timeout", "timeout"),
        ("device", "device_exception"),
        ("invalid", "invalid_write"),
        ("bad", "bad_request"),
        ("crash", "internal"),
        ("nope", "unknown_command"),
    ],
)
def test_errors_are_mapped(sock_path, cmd, kind):
    async def go():
        server = ControlServer(sock_path, 0o660, make_handlers())
        await server.start()
        try:
            with pytest.raises(DaemonError) as err:
                await call(sock_path, cmd)
            assert err.value.type == kind
        finally:
            await server.stop()

    run(go())


def test_error_type_mapping_is_ordered_for_subclasses():
    assert error_type(TimeoutError()) == "timeout"
    assert error_type(KeyError("x")) == "internal"


def test_malformed_requests_get_an_error_and_the_connection_survives(sock_path):
    async def go():
        server = ControlServer(sock_path, 0o660, make_handlers())
        await server.start()
        try:
            reader, writer = await asyncio.open_unix_connection(str(sock_path))
            for raw, kind in [
                (b"this is not json\n", "bad_request"),
                (b"[1,2]\n", "bad_request"),
                (b'{"id": 7}\n', "bad_request"),
                (b'{"id": 8, "cmd": "echo", "args": 5}\n', "bad_request"),
            ]:
                writer.write(raw)
                await writer.drain()
                response = ipc.decode(await reader.readline())
                assert response["ok"] is False and response["error"]["type"] == kind
            # the same connection still works, and the id is echoed
            writer.write(b'{"id": 9, "cmd": "echo", "args": {"k": "v"}}\n')
            await writer.drain()
            assert ipc.decode(await reader.readline()) == {"id": 9, "ok": True, "result": {"echo": {"k": "v"}}}
            writer.close()
        finally:
            await server.stop()

    run(go())


def test_several_requests_on_one_connection_and_concurrent_clients(sock_path):
    async def go():
        server = ControlServer(sock_path, 0o660, make_handlers())
        await server.start()
        try:
            reader, writer = await asyncio.open_unix_connection(str(sock_path))
            for i in range(3):
                writer.write(ipc.encode({"id": i, "cmd": "echo", "args": {"i": i}}))
                await writer.drain()
                assert ipc.decode(await reader.readline())["result"] == {"echo": {"i": i}}
            writer.close()
            results = await asyncio.gather(*(call(sock_path, "slow") for _ in range(5)))
            assert results == ["done"] * 5
        finally:
            await server.stop()

    run(go())


def test_oversized_request_is_rejected(sock_path):
    async def go():
        server = ControlServer(sock_path, 0o660, make_handlers())
        await server.start()
        try:
            reader, writer = await asyncio.open_unix_connection(str(sock_path))
            writer.write(b"x" * (ipc.MAX_REQUEST_BYTES + 10) + b"\n")
            await writer.drain()
            response = ipc.decode(await reader.readline())
            assert response["error"]["type"] == "bad_request"
            writer.close()
        finally:
            await server.stop()

    run(go())


def test_stop_closes_open_connections(sock_path):
    async def go():
        server = ControlServer(sock_path, 0o660, make_handlers())
        await server.start()
        reader, writer = await asyncio.open_unix_connection(str(sock_path))
        for _ in range(100):  # the server registers the connection asynchronously
            if server._clients:
                break
            await asyncio.sleep(0.01)
        await server.stop()
        assert await asyncio.wait_for(reader.read(), 2) == b""
        writer.close()

    run(go())


def test_client_errors_without_a_daemon(sock_path):
    with pytest.raises(DaemonUnreachable, match="no daemon socket"):
        ipc.request(sock_path, "status")
    sock_path.parent.mkdir(parents=True)
    import socket

    s = socket.socket(socket.AF_UNIX)
    s.bind(str(sock_path))  # bound but not listening
    with pytest.raises(DaemonUnreachable, match="not accepting"):
        ipc.request(sock_path, "status")
    s.close()


def test_client_timeout(sock_path):
    async def go():
        server = ControlServer(sock_path, 0o660, make_handlers())
        await server.start()
        try:
            with pytest.raises(DaemonUnreachable, match="did not answer"):
                await call(sock_path, "slow", timeout=0.05)
        finally:
            await server.stop()

    run(go())


def test_a_socket_path_that_is_too_long_is_a_clean_error(sock_path):
    from solarlife.errors import SolarlifeError

    async def go():
        server = ControlServer(sock_path.parent / ("x" * 120) / "s.sock", 0o660, make_handlers())
        with pytest.raises(SolarlifeError, match="cannot create control socket"):
            await server.start()

    run(go())


def test_the_server_and_the_client_also_talk_over_loopback_tcp():
    from fakes import free_port

    async def go():
        endpoint = f"tcp://127.0.0.1:{free_port()}"
        server = ControlServer(endpoint, 0o660, make_handlers())
        await server.start()
        try:
            assert await call(endpoint, "echo", {"a": 1}) == {"echo": {"a": 1}}
            with pytest.raises(DaemonError) as err:
                await call(endpoint, "not_connected")
            assert err.value.type == "not_connected"
        finally:
            await server.stop()
        with pytest.raises(DaemonUnreachable):
            await call(endpoint, "echo")

    run(go())


def test_a_bad_endpoint_is_reported_as_unreachable():
    with pytest.raises(DaemonUnreachable, match="loopback"):
        ipc.request("tcp://192.168.1.5:5000", "status")
