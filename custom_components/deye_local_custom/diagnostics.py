"""Diagnostics: the model, the connection, the values (no address, no serials), and
fingerprints of the installed Python packages (packages.py)."""

from __future__ import annotations

from time import monotonic
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant

from .const import CONF_LOGGER_SERIAL
from .coordinator import DeyeConfigEntry
from .packages import fingerprints

TO_REDACT = {CONF_HOST, CONF_LOGGER_SERIAL}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: DeyeConfigEntry
) -> dict[str, Any]:
    coordinator = entry.runtime_data
    now = monotonic()
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "model": coordinator.model.key,
        "connected": coordinator.connected,
        "last_error": coordinator.last_error,
        "requests": dict(coordinator.requests),
        "active": sorted(coordinator.active_keys()),
        "values": {
            key: {
                "value": value,
                "age_s": round(now - coordinator.read_at[key], 1),
            }
            for key, value in sorted(coordinator.values.items())
        },
        "packages": await hass.async_add_executor_job(fingerprints),
    }
