"""The client against the fake logger: reads, writes, failures, reconnecting."""

from __future__ import annotations

import ast
import asyncio
from collections.abc import AsyncGenerator, Awaitable
import errno
import inspect
import textwrap
import threading
import time

from pysolarmanv5 import PySolarmanV5Async
import pytest
import umodbus

from custom_components.deye_local_custom.client import (
    DeyeClient,
    DeyeConnectionError,
    DeyeModbusError,
    NotWritable,
    _Transport,
)

from .conftest import TIMEOUT
from .fake_logger import LOGGER_SERIAL, FakeLogger, s16

WRITABLE = {230: frozenset({30, 50})}


async def fails_at_once(request: Awaitable[object]) -> DeyeConnectionError:
    """The request fails well before the reply timeout."""
    started = time.monotonic()
    with pytest.raises(DeyeConnectionError) as err:
        await request
    assert time.monotonic() - started < TIMEOUT / 2
    return err.value


@pytest.fixture
async def client(logger: FakeLogger) -> AsyncGenerator[DeyeClient]:
    deye = DeyeClient("127.0.0.1", logger.port, LOGGER_SERIAL, WRITABLE)
    yield deye
    await deye.close()


async def test_read_and_write(client: DeyeClient, logger: FakeLogger) -> None:
    assert await client.read(150, 3) == [2305, 0, 0]
    await client.write(230, 30)
    assert await client.read(230, 1) == [30]
    assert logger.requests == [
        ("read", 150, 3),
        ("write", 230, [30]),
        ("read", 230, 1),
    ]
    assert logger.connections == 1  # one connection for all


@pytest.mark.parametrize(("register", "value"), [(230, 40), (230, 0), (210, 50)])
async def test_only_declared_values_are_written(
    client: DeyeClient, logger: FakeLogger, register: int, value: int
) -> None:
    with pytest.raises(NotWritable):
        await client.write(register, value)
    assert logger.requests == []
    assert logger.connections == 0  # not even connected


async def test_no_writes_without_a_model(logger: FakeLogger) -> None:
    deye = DeyeClient("127.0.0.1", logger.port, LOGGER_SERIAL, {})
    with pytest.raises(NotWritable):
        await deye.write(230, 50)
    await deye.close()


async def test_silence_drops_the_connection(
    client: DeyeClient, logger: FakeLogger
) -> None:
    logger.unanswered = 1
    with pytest.raises(DeyeConnectionError, match="TimeoutError"):
        await client.read(150, 1)
    assert await client.read(150, 1) == [2305]
    assert logger.connections == 2


async def test_the_logger_away_and_back(client: DeyeClient, logger: FakeLogger) -> None:
    assert await client.read(150, 1) == [2305]
    await logger.stop()
    with pytest.raises(DeyeConnectionError) as err:
        await client.read(150, 1)
    assert "127.0.0.1" not in str(err.value)
    await logger.start()
    assert await client.read(150, 1) == [2305]


async def test_a_closed_connection_is_reopened(
    client: DeyeClient, logger: FakeLogger
) -> None:
    assert await client.read(150, 1) == [2305]
    old = client._transport._socket_writer
    await logger.drop_connections()
    assert client._transport.writer is None  # pysolarmanv5 dropped it, unclosed
    assert await client.read(150, 1) == [2305]
    assert logger.connections == 2
    assert old.is_closing()  # closed by us


@pytest.mark.parametrize("how", ["fin", "rst"])
async def test_the_logger_hangs_up_on_a_request(
    client: DeyeClient, logger: FakeLogger, how: str
) -> None:
    """The request fails as the connection ends (3.0.6 left it to time out)."""
    assert await client.read(150, 1) == [2305]
    logger.hang_up = how
    await fails_at_once(client.read(150, 1))
    logger.hang_up = None
    assert await client.read(150, 1) == [2305]
    assert logger.connections == 2


async def test_a_reply_cut_short(client: DeyeClient, logger: FakeLogger) -> None:
    """Under 6 bytes from A5: 3.0.6's reader fails (IndexError) and keeps `writer`."""
    assert await client.read(150, 1) == [2305]
    logger.cut = 2
    await fails_at_once(client.read(150, 1))
    logger.cut = None
    assert await client.read(150, 1) == [2305]
    assert logger.connections == 2


async def test_a_socket_error_ends_the_connection(
    client: DeyeClient, logger: FakeLogger
) -> None:
    """An error other than a reset (as asyncio passes ETIMEDOUT to the reader): 3.0.6's
    reader fails with `writer` still set, and the next request would fail."""
    assert await client.read(150, 1) == [2305]
    transport = client._transport
    transport.reader.set_exception(TimeoutError(errno.ETIMEDOUT, "Timed out"))
    await asyncio.wait([transport.reader_task])
    assert transport.writer is None
    assert await client.read(150, 1) == [2305]  # on a new connection
    assert logger.connections == 2


async def test_close_wakes_a_waiting_request(logger: FakeLogger) -> None:
    deye = DeyeClient("127.0.0.1", logger.port, LOGGER_SERIAL, WRITABLE)
    logger.silent = True
    request = asyncio.create_task(deye.read(150, 1))
    await logger.received.wait()  # sent, waiting for the reply
    await deye.close()
    await fails_at_once(request)


async def test_close_while_connecting_leaves_nothing_open(
    logger: FakeLogger, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, gate = asyncio.Event(), asyncio.Event()
    connect = _Transport.connect

    async def held_connect(self: _Transport) -> None:
        entered.set()
        await gate.wait()
        await connect(self)

    monkeypatch.setattr(_Transport, "connect", held_connect)
    deye = DeyeClient("127.0.0.1", logger.port, LOGGER_SERIAL, WRITABLE)
    request = asyncio.create_task(deye.read(150, 1))
    await entered.wait()
    await deye.close()
    gate.set()
    with pytest.raises(DeyeConnectionError, match="closed"):
        await request
    assert deye._transport is None  # and no reader left running (lingering task)
    assert logger.requests == []


async def test_modbus_exception_keeps_the_connection(
    client: DeyeClient, logger: FakeLogger
) -> None:
    logger.exception = 2  # illegal data address
    with pytest.raises(DeyeModbusError, match="IllegalDataAddressError"):
        await client.read(150, 1)
    logger.exception = None
    assert await client.read(150, 1) == [2305]
    assert logger.connections == 1


async def test_unknown_modbus_exception_code(
    client: DeyeClient, logger: FakeLogger
) -> None:
    """umodbus has no class for code 7 and raises KeyError: still a failed request."""
    logger.exception = 7
    with pytest.raises(DeyeConnectionError, match="unexpected reply: KeyError"):
        await client.read(150, 1)
    logger.exception = None
    assert await client.read(150, 1) == [2305]


async def test_words_stay_unsigned_whatever_umodbus_is_set_to(
    client: DeyeClient, logger: FakeLogger, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(umodbus.conf, "SIGNED_VALUES", True)  # as the env variable
    assert await client.read(190, 2) == [s16(-1570), s16(-2950)]


async def test_wrong_logger_serial_gets_no_answer(logger: FakeLogger) -> None:
    deye = DeyeClient("127.0.0.1", logger.port, LOGGER_SERIAL + 1, WRITABLE)
    with pytest.raises(DeyeConnectionError):
        await deye.read(0, 18)
    await deye.close()


async def test_closed_client(client: DeyeClient, logger: FakeLogger) -> None:
    assert await client.read(150, 1) == [2305]
    await client.close()
    with pytest.raises(DeyeConnectionError, match="closed"):
        await client.read(150, 1)


async def test_request_size(client: DeyeClient) -> None:
    with pytest.raises(ValueError):
        await client.read(0, 126)


def _assigned(function) -> set[str]:
    tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
    names = set()
    for node in ast.walk(tree):
        targets = node.targets if isinstance(node, ast.Assign) else []
        if isinstance(node, ast.AnnAssign):
            targets = [node.target]
        names |= {
            t.attr
            for t in targets
            if isinstance(t, ast.Attribute)
            and isinstance(t.value, ast.Name)
            and t.value.id == "self"
        }
    return names


def test_transport_sets_what_pysolarmanv5_sets() -> None:
    """The constructor replaced to avoid multiprocessing must set the same attributes."""
    assert _assigned(_Transport.__init__) == _assigned(PySolarmanV5Async.__init__) | {
        "_socket_writer"
    }


async def test_transport_has_no_multiprocessing() -> None:
    transport = _Transport("127.0.0.1", LOGGER_SERIAL, socket_timeout=1)
    assert isinstance(transport.data_wanted_ev, threading.Event)
    assert transport._needs_reconnect is False
