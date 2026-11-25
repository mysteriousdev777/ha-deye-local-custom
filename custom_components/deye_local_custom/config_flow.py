"""Add the inverter (logger address, port, logger serial); reconfigure them; set the
reading intervals (options).

No network search: the logger's address and serial are entered by hand (its UDP
discovery answers anyone on the network, and the address should be fixed anyway).
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import callback
from homeassistant.data_entry_flow import AbortFlow
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .client import DeyeClient, DeyeError
from .const import (
    CONF_DEVICE_TYPE,
    CONF_LOGGER_SERIAL,
    DEFAULT_PORT,
    DOMAIN,
    INTERVAL_CHOICES,
    Tier,
)
from .description import interval_errors, intervals_from, intervals_in_use
from .models import INFO_COUNT, INFO_FIRST, find_model, parse_info
from .planner import requests_per_minute

_LOGGER = logging.getLogger(__name__)

SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=65535)
        ),
        vol.Required(CONF_LOGGER_SERIAL): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=0xFFFFFFFF)
        ),
    }
)

OPTIONS_SCHEMA = vol.Schema(
    {
        vol.Required(tier.option): SelectSelector(
            SelectSelectorConfig(
                options=[
                    SelectOptionDict(value=str(seconds), label=f"{seconds} s")
                    for seconds in INTERVAL_CHOICES[tier]
                ],
                mode=SelectSelectorMode.DROPDOWN,
            )
        )
        for tier in Tier
    }
)


class NoSerial(ValueError):
    """The inverter's serial number registers hold no serial."""


class DeyeLocalCustomConfigFlow(ConfigFlow, domain=DOMAIN):
    """Read the inverter's identity through the logger; check the model."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> DeyeOptionsFlow:
        return DeyeOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                title, data = await self._async_probe(user_input)
            except DeyeError:
                errors["base"] = "cannot_connect"
            except NoSerial:
                errors["base"] = "no_serial"
            else:
                return self.async_create_entry(title=title, data=data)
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(SCHEMA, user_input),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                _, data = await self._async_probe(user_input)
            except DeyeError:
                errors["base"] = "cannot_connect"
            except NoSerial:
                errors["base"] = "no_serial"
            else:
                return self.async_update_reload_and_abort(entry, data_updates=data)
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                SCHEMA, user_input or dict(entry.data)
            ),
            errors=errors,
        )

    async def _async_probe(
        self, user_input: dict[str, Any]
    ) -> tuple[str, dict[str, Any]]:
        """Title and entry data. Raises DeyeError (no answer), NoSerial, or AbortFlow
        (another inverter on reconfigure, one already added, a model not supported)."""
        host = user_input[CONF_HOST].strip()
        port = user_input[CONF_PORT]
        logger_serial = user_input[CONF_LOGGER_SERIAL]
        client = DeyeClient(host, port, logger_serial, writable={})
        try:
            words = await client.read(INFO_FIRST, INFO_COUNT)
        finally:
            await client.close()
        try:
            info = parse_info(words)
        except ValueError as err:
            raise NoSerial from err
        _LOGGER.debug(
            "Inverter %s: device type 0x%04X, rated %s W",
            info.serial,
            info.device_type,
            info.rated_power,
        )
        # The model first: an entry already added must not take a device type this
        # version cannot set up (the abort below would update it)
        model = find_model(info.device_type)
        if model is None:
            raise AbortFlow(
                "unsupported_model", {"device_type": f"0x{info.device_type:04X}"}
            )

        await self.async_set_unique_id(info.serial)
        data = {
            CONF_HOST: host,
            CONF_PORT: port,
            CONF_LOGGER_SERIAL: logger_serial,
            CONF_DEVICE_TYPE: info.device_type,
        }
        if self.source == SOURCE_RECONFIGURE:
            self._abort_if_unique_id_mismatch(reason="wrong_device")
        else:
            self._abort_if_unique_id_configured(updates=data)
        return model.name, data


class DeyeOptionsFlow(OptionsFlowWithReload):
    """Reading intervals by tier. The entry reloads when they change.

    The form starts from the intervals in use: the defaults when the stored ones are
    not valid (the selectors take only their choices).
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        options = self.config_entry.options
        errors: dict[str, str] = {}
        if user_input is not None:
            intervals = {tier: int(user_input[tier.option]) for tier in Tier}
            # Checked first: a combination stored by hand that is not valid must not
            # pass as "nothing changed"
            if problems := interval_errors(intervals):
                errors = {tier.option: problem for tier, problem in problems.items()}
            elif intervals == intervals_from(options):
                # Nothing changed (also the defaults saved over no options yet): the
                # options stay as they are, so the entry does not reload
                return self.async_create_entry(data=dict(options))
            else:
                return self.async_create_entry(
                    data={tier.option: seconds for tier, seconds in intervals.items()}
                )
        current = intervals_in_use(options)
        shown = user_input or {tier.option: str(s) for tier, s in current.items()}
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(OPTIONS_SCHEMA, shown),
            errors=errors,
            description_placeholders={"requests": self._requests_now(current)},
        )

    def _requests_now(self, intervals: dict[Tier, int]) -> str:
        """Requests a minute the plan makes now: with the enabled entities while the
        entry is loaded, with the default ones otherwise."""
        entry = self.config_entry
        if entry.state is ConfigEntryState.LOADED:
            coordinator = entry.runtime_data
            model, active = coordinator.model, coordinator.active_keys()
        else:
            found = find_model(entry.data[CONF_DEVICE_TYPE])
            if found is None:
                return "-"
            model = found.with_intervals(intervals)
            active = {
                d.key for d in model.descriptions if d.entity_registry_enabled_default
            }
        return f"{round(requests_per_minute(model, active), 1):g}"
