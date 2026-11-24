"""Add the inverter (logger address, port, logger serial); reconfigure them.

No network search: the logger's address and serial are entered by hand (its UDP
discovery answers anyone on the network, and the address should be fixed anyway).
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    ConfigFlow,
    ConfigFlowResult,
)
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.data_entry_flow import AbortFlow

from .client import DeyeClient, DeyeError
from .const import CONF_DEVICE_TYPE, CONF_LOGGER_SERIAL, DEFAULT_PORT, DOMAIN
from .models import INFO_COUNT, INFO_FIRST, find_model, parse_info

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


class NoSerial(ValueError):
    """The inverter's serial number registers hold no serial."""


class DeyeLocalCustomConfigFlow(ConfigFlow, domain=DOMAIN):
    """Read the inverter's identity through the logger; check the model."""

    VERSION = 1

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
