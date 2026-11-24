"""The supported models, and what every Deye hybrid reports about itself."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from ..description import Model, combine
from .sg03lp1 import SG03LP1

MODELS: tuple[Model, ...] = (SG03LP1,)

# Read once while adding the inverter: registers 0-17.
INFO_FIRST = 0
INFO_COUNT = 18
_DEVICE_TYPE = 0
_SERIAL = slice(3, 8)  # ASCII, two characters per register, high byte first
_RATED_POWER = slice(16, 18)  # 0.1 W, low word first


@dataclass(frozen=True)
class InverterInfo:
    device_type: int
    serial: str
    rated_power: float  # W


def parse_info(words: Sequence[int]) -> InverterInfo:
    """Registers INFO_FIRST.. as the inverter's identity; ValueError without a serial."""
    serial = "".join(chr(w >> 8) + chr(w & 0xFF) for w in words[_SERIAL]).strip("\0 ")
    if not serial or not serial.isascii() or not serial.isalnum():
        raise ValueError("no serial number in registers 3-7")
    return InverterInfo(
        device_type=words[_DEVICE_TYPE],
        serial=serial,
        rated_power=combine(words[_RATED_POWER]) / 10,
    )


def find_model(device_type: int) -> Model | None:
    """The model for a device type (register 0); None for one not supported."""
    for model in MODELS:
        if device_type in model.device_types:
            return model
    return None
