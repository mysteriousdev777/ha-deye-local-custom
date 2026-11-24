"""Adding the inverter and reconfiguring the connection."""

from __future__ import annotations

from collections.abc import Generator
from typing import Any
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.deye_local_custom.const import (
    CONF_DEVICE_TYPE,
    CONF_LOGGER_SERIAL,
    DOMAIN,
)
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from .fake_logger import INVERTER_SERIAL, LOGGER_SERIAL, FakeLogger, serial_words


@pytest.fixture(autouse=True)
def no_setup() -> Generator[None]:
    """The flow only: the entry is not set up."""
    with patch(
        "custom_components.deye_local_custom.async_setup_entry", return_value=True
    ):
        yield


@pytest.fixture
def user_input(logger: FakeLogger) -> dict[str, Any]:
    return {
        CONF_HOST: "127.0.0.1",
        CONF_PORT: logger.port,
        CONF_LOGGER_SERIAL: LOGGER_SERIAL,
    }


async def start(hass: HomeAssistant) -> dict[str, Any]:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    return result


async def test_add(
    hass: HomeAssistant,
    enable_custom_integrations: None,
    logger: FakeLogger,
    user_input: dict[str, Any],
) -> None:
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "SUN-5K-SG03LP1-EU"
    assert result["result"].unique_id == INVERTER_SERIAL
    assert result["data"] == {**user_input, CONF_DEVICE_TYPE: 3}
    assert logger.reads == [(0, 18)]


async def test_no_answer_then_fixed(
    hass: HomeAssistant,
    enable_custom_integrations: None,
    logger: FakeLogger,
    user_input: dict[str, Any],
) -> None:
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**user_input, CONF_LOGGER_SERIAL: LOGGER_SERIAL + 1}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_no_serial(
    hass: HomeAssistant,
    enable_custom_integrations: None,
    logger: FakeLogger,
    user_input: dict[str, Any],
) -> None:
    for register in range(3, 8):
        logger.registers[register] = 0
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input
    )
    assert result["errors"] == {"base": "no_serial"}


async def test_unsupported_model(
    hass: HomeAssistant,
    enable_custom_integrations: None,
    logger: FakeLogger,
    user_input: dict[str, Any],
) -> None:
    logger.registers[0] = 0x0006
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "unsupported_model"
    assert result["description_placeholders"] == {"device_type": "0x0006"}


async def test_already_added_gets_the_new_address(
    hass: HomeAssistant,
    enable_custom_integrations: None,
    logger: FakeLogger,
    user_input: dict[str, Any],
) -> None:
    old = MockConfigEntry(
        domain=DOMAIN,
        unique_id=INVERTER_SERIAL,
        data={**user_input, CONF_PORT: 1, CONF_DEVICE_TYPE: 3},
    )
    old.add_to_hass(hass)
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert old.data[CONF_PORT] == logger.port


async def test_already_added_keeps_its_model(
    hass: HomeAssistant,
    enable_custom_integrations: None,
    logger: FakeLogger,
    user_input: dict[str, Any],
) -> None:
    """An unsupported device type under a known serial: nothing in the entry changes
    (it would fail to set up after the update)."""
    data = {**user_input, CONF_PORT: 1, CONF_DEVICE_TYPE: 3}
    old = MockConfigEntry(domain=DOMAIN, unique_id=INVERTER_SERIAL, data=data)
    old.add_to_hass(hass)
    logger.registers[0] = 0x0006
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "unsupported_model"
    assert old.data == data


async def test_reconfigure(
    hass: HomeAssistant,
    enable_custom_integrations: None,
    logger: FakeLogger,
    user_input: dict[str, Any],
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=INVERTER_SERIAL,
        data={**user_input, CONF_PORT: 1, CONF_DEVICE_TYPE: 3},
    )
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_PORT] == logger.port


async def test_reconfigure_to_another_inverter(
    hass: HomeAssistant,
    enable_custom_integrations: None,
    logger: FakeLogger,
    user_input: dict[str, Any],
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=INVERTER_SERIAL,
        data={**user_input, CONF_PORT: 1, CONF_DEVICE_TYPE: 3},
    )
    entry.add_to_hass(hass)
    logger.registers.update(zip(range(3, 8), serial_words("2300000002"), strict=True))
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_device"
    assert entry.data[CONF_PORT] == 1
