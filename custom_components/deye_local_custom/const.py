"""Constants of the Deye Local Custom integration."""

from collections.abc import Mapping
from enum import StrEnum
from typing import Final

DOMAIN: Final = "deye_local_custom"

# Config entry data, besides CONF_HOST and CONF_PORT of homeassistant.const
CONF_LOGGER_SERIAL: Final = "logger_serial"  # of the logger, not the inverter
CONF_DEVICE_TYPE: Final = "device_type"  # register 0 of the inverter: picks the model

DEFAULT_PORT: Final = 8899

# Seconds to connect, and to wait for one reply. The logger answers in about a second,
# a few replies a day take over 10 s.
REQUEST_TIMEOUT: Final = 15.0

# A value stays available this long past its reading interval after its last good read
# (fast values by default: 10 + 90 s). The logger reboots every 3 hours and is away for
# 30-45 s: with the next poll and a timed-out request, about 70 s after the last good
# read in the worst case. Past it, the values go unavailable and the connection sensor
# goes off.
HOLD: Final = 90.0

# Writing a preset: an attempt (read, write if different, read back); while the logger
# does not answer, the next one WRITE_RETRY seconds after a failed one, the last one
# starting by WRITE_DEADLINE (an attempt waits up to REQUEST_TIMEOUT for each reply).
# The same value again is harmless if a reply was lost after the write went through.
WRITE_DEADLINE: Final = 60.0
WRITE_RETRY: Final = 10.0

# The only values the grid charging current (register 230) can be set to, amperes:
# 50 A - the everyday setting, proven over time; 30 A - the saving mode (less current
# from the grid while charging), not proven yet. The model keeps them within the
# inverter's limits (a test checks it).
GRID_CHARGE_PRESETS_A: Final = (30, 50)


class Tier(StrEnum):
    """How often a value is read. Each tier's interval is an option of the entry."""

    # Grid voltage alone: automations that report the grid going down react to it
    GRID_VOLTAGE = "grid_voltage"
    FAST = "fast"  # grid status, power, battery: one request
    SLOW = "slow"  # counters, temperatures, running status, settings

    @property
    def option(self) -> str:
        """Its key in the entry's options."""
        return f"{self.value}_interval"


# Reading intervals, seconds. The polling step is the grid voltage's interval, so each
# tier is a multiple of the one before it: the fast one is checked (the form), every
# slow choice is a multiple of every fast one, the clock's fixed 300 s of every grid
# voltage choice (a test keeps both). Every poll sends a request: about
# 60 / grid voltage + 60 / slow requests a minute; a fast interval longer than the grid
# voltage's makes the polls between the fast ones one-register requests (150), not
# fewer requests.
DEFAULT_INTERVALS: Final[Mapping[Tier, int]] = {
    Tier.GRID_VOLTAGE: 10,
    Tier.FAST: 10,
    Tier.SLOW: 60,
}
INTERVAL_CHOICES: Final[Mapping[Tier, tuple[int, ...]]] = {
    Tier.GRID_VOLTAGE: (5, 10, 15, 20, 30),
    Tier.FAST: (10, 15, 20, 30, 60),
    Tier.SLOW: (60, 120, 300),
}
