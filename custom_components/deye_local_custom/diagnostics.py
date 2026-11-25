"""Diagnostics: the model, the reading intervals, the connection, the values (no
address, no serials), and fingerprints of the installed Python packages (packages.py)."""

from __future__ import annotations

from time import monotonic
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant

from .const import CONF_LOGGER_SERIAL, Tier
from .coordinator import DeyeConfigEntry
from .packages import fingerprints
from .planner import requests_per_minute

TO_REDACT = {CONF_HOST, CONF_LOGGER_SERIAL}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: DeyeConfigEntry
) -> dict[str, Any]:
    coordinator = entry.runtime_data
    model = coordinator.model
    now = monotonic()
    active = coordinator.active_keys()
    intervals = {d.tier: d.interval for d in model.descriptions if d.tier is not None}
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "options": dict(entry.options),
        "model": model.key,
        # In use: the options, or the defaults if they are not valid
        "intervals": {str(tier): intervals[tier] for tier in Tier if tier in intervals},
        "tick": model.tick,
        "planned_requests_per_minute": round(requests_per_minute(model, active), 1),
        "connected": coordinator.connected,
        "last_error": coordinator.last_error,
        "requests": dict(coordinator.requests),
        "active": sorted(active),
        "values": {
            key: {
                "value": value,
                "age_s": round(now - coordinator.read_at[key], 1),
            }
            for key, value in sorted(coordinator.values.items())
        },
        "packages": await hass.async_add_executor_job(fingerprints),
    }
