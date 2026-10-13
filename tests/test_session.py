import asyncio

import pytest

from fakes import FakeDevice, FakeLink
from solarlife.errors import (
    BusyError,
    DeviceException,
    InvalidWriteError,
    NotConnectedError,
    ProtocolError,
    TransactionTimeout,
)
from solarlife.protocol import Result
from solarlife.session import DeviceSession
from solarlife.variables import variables


def make(device=None, **kwargs):
    device = device or FakeDevice()
    link = FakeLink("AA:BB:CC:DD:EE:FF", device)
    kwargs.setdefault("request_timeout_s", 0.3)
    kwargs.setdefault("attempts", 3)
    session = DeviceSession(link, **kwargs)
    return device, link, session


async def attached(**kwargs):
    device, link, session = make(**kwargs)
    await session.attach()
    return device, link, session


def run(coro):
    return asyncio.run(coro)


def test_fragmented_status_read():
    async def go():
        device, link, session = await attached()
        results = await session.read_block(0x3030, 41)
        assert len(results) == 44
        assert results["equipment_id"].value == 1
        assert results["battery_voltage_level"].value == 24.0
        assert len(link.writes) == 1

    run(go())


def test_parameter_read():
    async def go():
        _, _, session = await attached()
        results = await session.read_block(0x9021, 12)
        assert len(results) == 12
        assert results["battery_type"].value == "Lithium"

    run(go())


def test_unsolicited_while_idle_is_dropped_and_counted():
    async def go():
        _, link, session = await attached()
        link.inject(b"\x01\x04\x02\x00\x01")
        link.inject(b"\xde\xad")
        assert session.unsolicited == 2
        assert session.last_unsolicited_hex == "dead"
        assert len(await session.read_block(0x3030, 41)) == 44

    run(go())


def test_unsolicited_before_response_does_not_corrupt_it():
    async def go():
        device, link, session = await attached()
        device.prelude = [b"\xaa\xbb\xcc\xdd", b"\x01\x06\x00\x01"]
        results = await session.read_block(0x3030, 41)
        assert len(results) == 44
        assert session.unsolicited == 2
        assert len(link.writes) == 1

    run(go())


def test_bad_crc_is_retried():
    async def go():
        device, link, session = await attached()
        device.corrupt_crc_next = 1
        results = await session.read_block(0x3030, 41)
        assert len(results) == 44
        assert len(link.writes) == 2

    run(go())


def test_bad_crc_every_time_raises_protocol_error():
    async def go():
        device, link, session = await attached(attempts=2)
        device.corrupt_crc_next = 5
        with pytest.raises(ProtocolError, match="CRC"):
            await session.read_block(0x3030, 41)
        assert len(link.writes) == 2

    run(go())


def test_no_answer_raises_timeout_after_all_attempts():
    async def go():
        device, link, session = await attached(request_timeout_s=0.05)
        device.drop_next = 10
        with pytest.raises(TransactionTimeout, match="attempt 3/3"):
            await session.read_block(0x3030, 41)
        assert len(link.writes) == 3

    run(go())


def test_partial_response_is_never_returned():
    async def go():
        device, link, session = await attached(request_timeout_s=0.05)
        device.truncate_next = 10
        with pytest.raises(TransactionTimeout) as err:
            await session.read_block(0x3030, 41)
        assert "received: 0104" in str(err.value)

    run(go())


def test_exception_frame_raises_and_is_not_retried():
    async def go():
        device, link, session = await attached()
        device.exception_code_next = 0x02
        with pytest.raises(DeviceException) as err:
            await session.read_block(0x3030, 41)
        assert err.value.code == 2
        assert len(link.writes) == 1

    run(go())


def test_wrong_announced_length_is_a_protocol_error():
    async def go():
        device, link, session = await attached(attempts=1)
        from solarlife.crc import crc16

        def short(request):
            body = bytes([1, 4, 2, 0, 1])
            return body + crc16(body)

        device.override = short
        with pytest.raises(ProtocolError, match="announces 2"):
            await session.read_block(0x3030, 41)

    run(go())


def test_write_range_updates_the_device_and_validates_the_echo():
    async def go():
        device, link, session = await attached()
        params = await session.read_block(0x9021, 12)
        results = [Result(**vars(variables["float_voltage"]), value=13.8)]
        await session.write(results)
        assert device.registers[0x9026] == 1380
        assert link.writes[-1][1] in (0x06, 0x10)
        assert len(params) == 12

    run(go())


def test_write_with_invalid_values_raises_before_sending():
    async def go():
        _, link, session = await attached()
        with pytest.raises(InvalidWriteError):
            await session.write([])
        assert link.writes == []

    run(go())


def test_concurrent_requests_are_serialized():
    async def go():
        _, link, session = await attached()
        await asyncio.gather(
            session.read_block(0x3030, 41),
            session.read_block(0x9021, 12),
            session.read_block(0x3030, 41),
        )
        assert link.max_in_flight == 1
        assert len(link.writes) == 3

    run(go())


def test_disconnect_during_request_fails_without_retry():
    async def go():
        device, link, session = await attached(request_timeout_s=2)
        device.drop_next = 1
        task = asyncio.create_task(session.read_block(0x3030, 41))
        await asyncio.sleep(0.05)
        link.connected = False
        session.on_disconnect()
        with pytest.raises(NotConnectedError):
            await task
        assert len(link.writes) == 1

    run(go())


def test_request_when_not_connected():
    async def go():
        _, link, session = await attached()
        link.connected = False
        with pytest.raises(NotConnectedError):
            await session.read_block(0x3030, 41)

    run(go())


def test_busy_when_lock_is_held_too_long():
    async def go():
        device, link, session = await attached(lock_timeout_s=0.05, request_timeout_s=1)
        device.drop_next = 1
        first = asyncio.create_task(session.read_block(0x3030, 41))
        await asyncio.sleep(0.01)
        with pytest.raises(BusyError):
            await session.read_block(0x9021, 12)
        assert len(await first) == 44

    run(go())


def test_invalid_read_range():
    async def go():
        _, _, session = await attached()
        with pytest.raises(ProtocolError, match="invalid read range"):
            await session.read_block(0x7000, 1)

    run(go())
