"""The logger connection: Modbus RTU in Solarman V5 frames over TCP (pysolarmanv5).

One connection, opened on demand and dropped after a failure (the next request opens a
new one). Not for concurrent use: the coordinator runs one request at a time.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
import errno
import logging
import struct
import threading
from typing import Any

from pysolarmanv5 import (
    NoSocketAvailableError,
    PySolarmanV5,
    PySolarmanV5Async,
    V5FrameError,
)
from umodbus.client.serial.redundancy_check import CRCError
from umodbus.exceptions import ModbusError

from .const import REQUEST_TIMEOUT
from .description import MAX_REQUEST

_LOGGER = logging.getLogger(__name__)
# pysolarmanv5 logs every frame (with the logger's serial) at debug level
_TRANSPORT_LOGGER = _LOGGER.getChild("frames")


class DeyeError(Exception):
    """A request failed. The text never holds the address or serial numbers."""


class DeyeConnectionError(DeyeError):
    """No answer, or none that makes sense: the logger is away, rebooting or busy."""


class DeyeModbusError(DeyeError):
    """The inverter answered with a Modbus exception: it refuses the request."""


class NotWritable(DeyeError):
    """A register or value the model does not allow to write. Nothing was sent."""


def reason(err: BaseException) -> str:
    """What failed, without the address that exception texts may carry."""
    if isinstance(err, NoSocketAvailableError) and err.__cause__ is not None:
        err = err.__cause__
    if isinstance(err, V5FrameError):
        return f"V5FrameError: {err}"  # fixed texts in pysolarmanv5
    name = type(err).__name__
    if isinstance(err, OSError) and err.errno in errno.errorcode:
        name += f" ({errno.errorcode[err.errno]})"
    return name


class _Transport(PySolarmanV5Async):
    """pysolarmanv5's asyncio client, three things changed.

    - No multiprocessing.Event: 3.0.6 creates one for each client, and with Python
      3.14's default start method (forkserver) its semaphores start multiprocessing's
      resource tracker process, from the event loop. Asyncio needs a flag only. The
      constructor sets the attributes of PySolarmanV5Async.__init__ in 3.0.6, plus
      `_socket_writer` (a test compares them).
    - The socket reader always ends the connection and wakes a waiting request. In
      3.0.6 a socket error other than a reset, or a piece of a reply under 6 bytes
      starting with A5 (IndexError), ends the reader with `writer` still set; and when
      the reader ends, a request waiting for its reply is left to time out
      (`_send_data(b"")` is commented out there).
    - The socket is closed also after the logger closed the connection: the reader then
      drops `writer` without closing it, and disconnect() has nothing to close.
    """

    def __init__(self, address: str, serial: int, **kwargs: Any) -> None:
        PySolarmanV5.__init__(self, address, serial, **kwargs)
        self._needs_reconnect = False
        self.reader = None
        self.writer = None
        self.data_queue = asyncio.Queue(maxsize=1)
        self.data_wanted_ev = threading.Event()
        self.reader_task = None
        self._socket_writer: asyncio.StreamWriter | None = None

    async def connect(self) -> None:
        await super().connect()
        self._socket_writer = self.writer

    async def disconnect(self) -> None:
        try:
            await super().disconnect()
        finally:
            writer = self._socket_writer
            if writer is not None and not writer.is_closing():
                writer.close()

    async def _conn_keeper(self) -> None:
        try:
            await super()._conn_keeper()
        except Exception as err:
            _LOGGER.debug("The socket reader stopped: %s", reason(err))
            self.reader = None
            self.writer = None  # the next request opens a new connection
        finally:
            # A request waiting for its reply fails now (an empty reply is
            # NoSocketAvailableError), not at the timeout; also when disconnect()
            # cancels the reader. Nothing happens if no request waits.
            self._send_data(b"")


class DeyeClient:
    """Reads and writes holding registers of the inverter behind one logger."""

    def __init__(
        self,
        host: str,
        port: int,
        logger_serial: int,
        writable: Mapping[int, frozenset[int]],
    ) -> None:
        self.host = host
        self.port = port
        self.logger_serial = logger_serial
        self._writable = writable
        self._transport: _Transport | None = None
        self.closed = False

    async def read(self, first: int, count: int) -> list[int]:
        """Holding registers first..first + count - 1 (function 3)."""
        if not 1 <= count <= MAX_REQUEST:
            raise ValueError(f"{count} registers in one request")
        values = await self._request(lambda t: t.read_holding_registers(first, count))
        if not isinstance(values, list) or len(values) != count:
            raise DeyeConnectionError(f"no list of {count} registers in the reply")
        # Unsigned 16-bit words whatever umodbus's global signedness: its config is
        # read from the environment (UMODBUS_SIGNED_VALUES) and shared by anything in
        # the process that uses umodbus. The model decodes signs itself.
        return [value & 0xFFFF for value in values]

    async def write(self, register: int, value: int) -> None:
        """One register (function 16, as the inverter takes writes): only a value the
        model declares for it."""
        if value not in self._writable.get(register, ()):
            raise NotWritable(f"register {register} may not be set to {value}")
        await self._request(
            lambda t: t.write_multiple_holding_registers(register, [value])
        )

    async def close(self) -> None:
        self.closed = True
        await self._drop()

    async def _request(self, call: Callable[[_Transport], Awaitable[Any]]) -> Any:
        if self.closed:
            raise DeyeConnectionError("closed")
        transport = await self._connect()
        try:
            return await call(transport)
        except ModbusError as err:
            raise DeyeModbusError(type(err).__name__) from err
        except (
            NoSocketAvailableError,
            V5FrameError,
            TimeoutError,
            OSError,
            CRCError,
            struct.error,
            IndexError,
        ) as err:
            # A late reply would be dropped anyway (sequence number); a fresh
            # connection also gets past a half-open one after the logger rebooted.
            await self._drop()
            raise DeyeConnectionError(reason(err)) from err
        except Exception as err:
            # A reply umodbus cannot make sense of: a Modbus exception code it does
            # not know (7, 9 and above: KeyError), another function's reply. Still a
            # failed request: the coordinator must keep counting its hold time.
            _LOGGER.debug("Unexpected reply", exc_info=True)
            await self._drop()
            raise DeyeConnectionError(f"unexpected reply: {reason(err)}") from err

    async def _connect(self) -> _Transport:
        transport = self._transport
        # The reader clears `writer` when the connection ends: closed by the logger, a
        # socket error, a reply it cannot take
        if transport is not None and transport.writer is not None:
            return transport
        await self._drop()
        transport = _Transport(
            self.host,
            self.logger_serial,
            port=self.port,
            socket_timeout=REQUEST_TIMEOUT,
            logger=_TRANSPORT_LOGGER,
        )
        try:
            await transport.connect()
        except NoSocketAvailableError as err:
            raise DeyeConnectionError(reason(err)) from err
        self._transport = transport
        if self.closed:  # close() came while connecting: leave nothing open
            await self._drop()
            raise DeyeConnectionError("closed")
        return transport

    async def _drop(self) -> None:
        transport, self._transport = self._transport, None
        if transport is None:
            return
        reader = transport.reader_task
        try:
            await transport.disconnect()  # cancels the reader, does not wait for it
        except OSError as err:  # a broken pipe on the farewell write: closed anyway
            _LOGGER.debug("Closing the connection: %s", reason(err))
        if reader is not None:
            await asyncio.wait([reader])
