"""Deye Local Custom: one Deye hybrid inverter through its Solarman logger, locally."""

from __future__ import annotations

import logging

from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError

from .client import DeyeClient
from .const import CONF_DEVICE_TYPE, CONF_LOGGER_SERIAL, DOMAIN
from .coordinator import DeyeConfigEntry, DeyeCoordinator
from .description import interval_errors, intervals_from, intervals_in_use
from .models import find_model

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.SELECT, Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: DeyeConfigEntry) -> bool:
    data = entry.data
    model = find_model(data[CONF_DEVICE_TYPE])
    if model is None:  # only if a later version drops a model
        raise ConfigEntryError(
            translation_domain=DOMAIN,
            translation_key="unsupported_model",
            translation_placeholders={"device_type": f"0x{data[CONF_DEVICE_TYPE]:04X}"},
        )
    # Not valid only if a later version drops a choice, or the options were edited
    stored = intervals_from(entry.options)
    if errors := interval_errors(stored):
        _LOGGER.warning(
            "Reading intervals %s are not valid (%s): reading at the defaults",
            {str(tier): entry.options.get(tier.option) for tier in stored},
            ", ".join(f"{tier}: {error}" for tier, error in errors.items()),
        )
    model = model.with_intervals(intervals_in_use(entry.options))
    client = DeyeClient(
        data[CONF_HOST], data[CONF_PORT], data[CONF_LOGGER_SERIAL], model.writable
    )
    # Also when the first refresh fails: no connection left behind
    entry.async_on_unload(client.close)
    coordinator = DeyeCoordinator(hass, entry, client, model)
    # The first read is before the entities exist: they start with fresh values, and
    # with the logger away setup is retried (ConfigEntryNotReady).
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: DeyeConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
