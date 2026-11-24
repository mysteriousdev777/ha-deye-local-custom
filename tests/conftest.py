"""Fixtures: a fake logger on a local port, a clock under control, a set-up entry."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.deye_local_custom import client, coordinator, diagnostics
from custom_components.deye_local_custom.const import (
    CONF_DEVICE_TYPE,
    CONF_LOGGER_SERIAL,
    DOMAIN,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant

from .fake_logger import DEVICE_TYPE, INVERTER_SERIAL, LOGGER_SERIAL, FakeLogger

TIMEOUT = 0.3  # seconds: one reply, in tests


class Clock:
    """time.monotonic for the coordinator: moves only when a test says so."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    tick = Clock()
    monkeypatch.setattr(coordinator, "monotonic", tick)
    monkeypatch.setattr(diagnostics, "monotonic", tick)
    return tick


@pytest.fixture
async def logger(
    socket_enabled: None, monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[FakeLogger]:
    monkeypatch.setattr(client, "REQUEST_TIMEOUT", TIMEOUT)
    fake = FakeLogger()
    await fake.start()
    yield fake
    await fake.stop()


@pytest.fixture
def entry_data(logger: FakeLogger) -> dict[str, Any]:
    return {
        CONF_HOST: "127.0.0.1",
        CONF_PORT: logger.port,
        CONF_LOGGER_SERIAL: LOGGER_SERIAL,
        CONF_DEVICE_TYPE: DEVICE_TYPE,
    }


@pytest.fixture
def config_entry(entry_data: dict[str, Any]) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="SUN-5K-SG03LP1-EU",
        unique_id=INVERTER_SERIAL,
        data=entry_data,
    )


@pytest.fixture
async def entry(
    hass: HomeAssistant,
    enable_custom_integrations: None,
    config_entry: MockConfigEntry,
    clock: Clock,
) -> AsyncGenerator[MockConfigEntry]:
    """The entry, set up; unloaded at the end so its connection closes."""
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    yield config_entry
    if config_entry.state is ConfigEntryState.LOADED:
        await hass.config_entries.async_unload(config_entry.entry_id)
        await hass.async_block_till_done()
