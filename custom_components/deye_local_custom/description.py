"""How a model is described: registers, decoding, request blocks, writable registers.

A model is data in Python, not a profile file: what the integration may write must not
depend on a file anyone can edit. Each entity description carries its registers, how to
decode them and how often to read them; the model lists the request blocks and declares
its writable registers with their only values. The engine (planner, coordinator,
entities) is the same for any model.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from functools import cached_property
import math
from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntityDescription
from homeassistant.components.select import SelectEntityDescription
from homeassistant.components.sensor import SensorEntityDescription
from homeassistant.util import dt as dt_util

type Decoder = Callable[[Sequence[int]], Any]

MAX_REQUEST = 125  # registers in one Modbus read request


# Decoders: register values (in the order of the description's registers) to a state.
# None means a value the model does not know: the entity shows unknown, the coordinator
# logs it once.


def combine(words: Sequence[int], *, signed: bool = False) -> int:
    """Registers as one integer, the first one lowest (Deye's order for 32 bits)."""
    value = 0
    for i, word in enumerate(words):
        value |= (word & 0xFFFF) << (16 * i)
    bits = 16 * len(words)
    if signed and value >= 1 << (bits - 1):
        value -= 1 << bits
    return value


def number(scale: float = 1, *, signed: bool = False, offset: int = 0) -> Decoder:
    """(raw - offset) * scale, rounded to the scale's decimals."""
    # normalize(): 1.0 is a whole scale too (Decimal("1.0") has one decimal)
    digits = max(0, -int(Decimal(str(scale)).normalize().as_tuple().exponent))

    def decode(words: Sequence[int]) -> int | float:
        value = (combine(words, signed=signed) - offset) * scale
        return round(value, digits) if digits else int(value)

    return decode


def lookup(states: Mapping[int, str]) -> Decoder:
    """A state name for each known raw value."""

    def decode(words: Sequence[int]) -> str | None:
        return states.get(words[0])

    return decode


def bit(index: int) -> Decoder:
    def decode(words: Sequence[int]) -> bool:
        return bool(words[0] >> index & 1)

    return decode


def fault_codes(words: Sequence[int]) -> str:
    """Set bits as the codes the inverter shows: bit 0 is F01; "OK" for none."""
    value = combine(words)
    codes = [f"F{i + 1:02d}" for i in range(16 * len(words)) if value >> i & 1]
    return ", ".join(codes) or "OK"


def hex_code(words: Sequence[int]) -> str:
    """The raw bits in hex; "OK" for none."""
    value = combine(words)
    return f"0x{value:0{4 * len(words)}X}" if value else "OK"


def clock_drift(words: Sequence[int]) -> int | None:
    """Seconds the inverter's clock is ahead of Home Assistant's, both local time.

    Registers: (year - 2000, month), (day, hour), (minute, second), high byte first.
    Includes the reply's delay, under a second as a rule.
    """
    try:
        inverter = datetime(
            2000 + (words[0] >> 8),
            words[0] & 0xFF,
            words[1] >> 8,
            words[1] & 0xFF,
            words[2] >> 8,
            words[2] & 0xFF,
        )
    except ValueError:
        return None
    now = dt_util.now().replace(tzinfo=None, microsecond=0)
    return round((inverter - now).total_seconds())


# Entity descriptions: Home Assistant's, plus where the value is.


@dataclass(frozen=True, kw_only=True)
class RegisterDescription:
    """An entity's registers, low word first, and its reading interval in seconds.

    The value stays available for interval + HOLD after its last good read.
    """

    registers: tuple[int, ...]
    interval: int
    decode: Decoder


@dataclass(frozen=True, kw_only=True)
class DeyeSensorDescription(SensorEntityDescription, RegisterDescription):
    """A sensor; `state_icons`: icons of some of its states.

    Default icons are in icons.json, found by the translation key (names come from
    `name`: there are no entity names in translations). Icons by state cannot go
    there: icons.json takes lowercase state keys only, and these states keep the
    previous integration's spelling (On-Grid).
    """

    state_icons: Mapping[str, str] | None = None


@dataclass(frozen=True, kw_only=True)
class DeyeBinarySensorDescription(BinarySensorEntityDescription, RegisterDescription):
    """A binary sensor."""


@dataclass(frozen=True, kw_only=True)
class DeyeSelectDescription(SelectEntityDescription, RegisterDescription):
    """A writable register (one) with a fixed list of values: the only thing written.

    `values` pairs with `options`; `limits` is what the inverter itself accepts for the
    register (a test keeps the values within it).
    """

    values: tuple[int, ...]
    limits: tuple[int, int]


type AnyDescription = (
    DeyeSensorDescription | DeyeBinarySensorDescription | DeyeSelectDescription
)


@dataclass(frozen=True)
class Block:
    """Registers read with one request: the due ones in it, lowest to highest."""

    first: int
    last: int

    def holds(self, description: RegisterDescription) -> bool:
        return (
            self.first <= min(description.registers)
            and max(description.registers) <= self.last
        )


@dataclass(frozen=True, kw_only=True)
class Model:
    """One inverter model: its entities, request blocks and writable registers."""

    key: str
    manufacturer: str
    name: str  # model name of the device
    device_name: str  # device name in Home Assistant: entity ids start with it
    device_types: frozenset[int]  # values of register 0 this model takes
    blocks: tuple[Block, ...]  # in request order
    sensors: tuple[DeyeSensorDescription, ...] = ()
    binary_sensors: tuple[DeyeBinarySensorDescription, ...] = ()
    selects: tuple[DeyeSelectDescription, ...] = ()

    @cached_property
    def descriptions(self) -> tuple[AnyDescription, ...]:
        return (*self.sensors, *self.binary_sensors, *self.selects)

    @cached_property
    def writable(self) -> Mapping[int, frozenset[int]]:
        """Register -> the only values it may be set to."""
        return {d.registers[0]: frozenset(d.values) for d in self.selects}

    @cached_property
    def tick(self) -> int:
        """Polling step, seconds: every reading interval is a multiple of it."""
        return math.gcd(*(d.interval for d in self.descriptions))
