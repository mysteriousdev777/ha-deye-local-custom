"""Constants of the Deye Local Custom integration."""

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
# (fast values: 10 + 90 s). The logger reboots every 3 hours and is away for 30-45 s:
# with the next poll and a timed-out request, about 70 s after the last good read in
# the worst case. Past it, the values go unavailable and the connection sensor goes off.
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

# Reading interval of the grid voltage, seconds. Automations that report the grid going
# down react to it. At 10 s it goes with the other fast values in one request; 5 s
# would add a one-register request between them - to be tried after the move, watching
# the logger's load.
GRID_VOLTAGE_INTERVAL_S: Final = 10
