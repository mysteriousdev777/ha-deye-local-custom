"""A fake Solarman logger with a Deye inverter behind it, on a local TCP port.

Solarman V5 frames with Modbus RTU inside, as pysolarmanv5 sends and expects them:

  A5 | length u16 LE | control u16 LE | sequence | 00 | logger serial u32 LE |
     payload | checksum | 15

A request's payload: frame type 02, sensor type (2), three times (4 each), the Modbus
frame. A reply (control 0x1510, the request's sequence number): frame type 02, status
01, three times, the Modbus frame. The serial numbers are made up.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import struct
from typing import Literal

from umodbus.client.serial.redundancy_check import get_crc

LOGGER_SERIAL = 1234567890
INVERTER_SERIAL = "2300000001"
DEVICE_TYPE = 0x0003


def serial_words(serial: str) -> list[int]:
    text = serial.ljust(10, "\0")
    return [ord(text[i]) << 8 | ord(text[i + 1]) for i in range(0, 10, 2)]


def s16(value: int) -> int:
    return value & 0xFFFF


def default_registers() -> dict[int, int]:
    """A sunny-day snapshot: grid on, battery charging from the grid at 50 A max."""
    registers = {
        0: DEVICE_TYPE,
        16: 50000,  # rated power 5000.0 W
        17: 0,
        22: 26 << 8 | 11,  # 2026-11
        23: 24 << 8 | 12,  # 24th, 12 h
        24: 0 << 8 | 30,  # 00 min 30 s
        59: 2,  # Normal
        70: 52,
        71: 31,
        72: 0x2345,  # total battery charge 0x12345 = 74565 -> 7456.5 kWh
        73: 0x0001,
        74: 30000,  # 3000.0 kWh
        75: 0,
        76: 81,
        77: 0,
        78: 0x5678,  # total bought: 0x15678 = 87672 -> 8767.2 kWh
        79: 5003,  # grid frequency 50.03 Hz
        80: 0x0001,
        84: 123,
        85: 45678,  # total load consumption 4567.8 kWh
        86: 0,
        90: 1350,  # 35.0 C
        91: 1412,  # 41.2 C
        150: 2305,  # grid 230.5 V
        157: 2299,
        160: 512,
        164: 1203,
        167: 1180,
        169: 1180,  # from the grid
        173: 2750,
        175: 2750,
        176: 2750,
        178: 2750,
        182: 1215,  # 21.5 C
        183: 5321,  # 53.21 V
        184: 87,
        190: s16(-1570),  # charging
        191: s16(-2950),  # -29.5 A
        192: 5000,
        194: 1,  # On-Grid
        210: 120,
        211: 120,
        230: 50,
        232: 1,
    }
    registers.update(zip(range(3, 8), serial_words(INVERTER_SERIAL), strict=True))
    return registers


def v5_reply(sequence: int, serial: int, modbus: bytes) -> bytes:
    payload = bytes([0x02, 0x01]) + bytes(12) + modbus
    frame = bytearray(
        b"\xa5"
        + struct.pack("<H", len(payload))
        + b"\x10\x15"
        + bytes([sequence, 0])
        + struct.pack("<I", serial)
        + payload
        + b"\x00\x15"
    )
    frame[-2] = sum(frame[1:-2]) & 0xFF
    return bytes(frame)


class FakeLogger:
    """Answers reads (function 3) and writes (function 16) of holding registers.

    Knobs: `silent` (pass nothing to the inverter, answer nothing), `unanswered` (carry
    out the next N requests but lose their replies), `lose` (the same for these numbers
    in `requests`), `delay` (seconds before each
    reply), `exception` (answer with this Modbus exception code), `ignore_writes`
    (acknowledge writes, keep the old values), `cut` (send only the first N bytes of
    each reply), `hang_up` ("fin" or "rst": close the connection instead of replying).
    """

    def __init__(self) -> None:
        self.serial = LOGGER_SERIAL
        self.registers = default_registers()
        self.requests: list[tuple[str, int, int | list[int]]] = []
        self.received = asyncio.Event()  # set on each request
        self.silent = False
        self.unanswered = 0
        self.delay = 0.0
        self.exception: int | None = None
        self.ignore_writes = False
        self.lose: set[int] = set()  # numbers in `requests` whose replies are lost
        self.cut: int | None = None
        self.hang_up: Literal["fin", "rst"] | None = None
        self.connections = 0
        self.in_flight = 0
        self.max_in_flight = 0
        self.port = 0
        self._server: asyncio.Server | None = None
        self._writers: set[asyncio.StreamWriter] = set()
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def reads(self) -> list[tuple[int, int]]:
        return [(first, n) for kind, first, n in self.requests if kind == "read"]  # type: ignore[misc]

    async def start(self) -> None:
        """Listen (again, on the same port after stop())."""
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", self.port)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        """As a reboot: the connections drop and the port is closed."""
        if self._server is None:
            return
        self._server.close()
        for writer in list(self._writers):
            writer.close()
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self._server.wait_closed()
        self._server = None

    async def drop_connections(self) -> None:
        """The logger closes its connections but keeps listening."""
        for writer in list(self._writers):
            writer.close()
        await asyncio.sleep(0.05)

    async def _serve(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.connections += 1
        self._writers.add(writer)
        buffer = bytearray()
        try:
            while data := await reader.read(1024):
                buffer += data
                while len(buffer) >= 3:
                    size = 13 + struct.unpack_from("<H", buffer, 1)[0]
                    if len(buffer) < size:
                        break
                    frame = bytes(buffer[:size])
                    del buffer[:size]
                    task = asyncio.create_task(self._answer(frame, writer))
                    self._tasks.add(task)
                    task.add_done_callback(self._tasks.discard)
        except ConnectionError:
            pass
        finally:
            self._writers.discard(writer)
            writer.close()

    async def _answer(self, frame: bytes, writer: asyncio.StreamWriter) -> None:
        if struct.unpack_from("<I", frame, 7)[0] != self.serial:
            return  # a frame for another logger: no answer
        modbus = frame[26:-2]
        function = modbus[1]
        if function not in (3, 16):
            return
        first, count = struct.unpack_from(">HH", modbus, 2)
        values = (
            list(struct.unpack_from(f">{count}H", modbus, 7)) if function == 16 else []
        )
        number = len(self.requests)
        self.requests.append(
            ("read", first, count) if function == 3 else ("write", first, values)
        )
        self.received.set()
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.silent:  # nothing reaches the inverter
                return
            if self.delay:
                await asyncio.sleep(self.delay)
            pdu = self._carry_out(modbus[0], function, first, count, values)
            if self.unanswered or number in self.lose:  # done, the reply is lost
                self.unanswered = max(self.unanswered - 1, 0)
                return
            if self.hang_up == "rst":  # SO_LINGER 0: close() sends a reset
                linger = struct.pack("ii", 1, 0)
                sock = writer.get_extra_info("socket")
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, linger)
                writer.transport.abort()
                return
            if self.hang_up == "fin":
                writer.close()
                return
            reply = v5_reply(frame[5], self.serial, pdu + get_crc(pdu))
            with contextlib.suppress(ConnectionError):
                writer.write(reply[: self.cut])
                await writer.drain()
        finally:
            self.in_flight -= 1

    def _carry_out(
        self, slave: int, function: int, first: int, count: int, values: list[int]
    ) -> bytes:
        """The inverter's Modbus reply, without the CRC."""
        if self.exception is not None:
            return bytes([slave, function | 0x80, self.exception])
        if function == 3:
            data = b"".join(
                struct.pack(">H", self.registers.get(a, 0))
                for a in range(first, first + count)
            )
            return bytes([slave, 3, len(data)]) + data
        if not self.ignore_writes:
            for i, value in enumerate(values):
                self.registers[first + i] = value
        return bytes([slave, 16]) + struct.pack(">HH", first, count)
