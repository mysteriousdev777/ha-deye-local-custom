"""Set-up, entities, polling, reading intervals, values kept through gaps,
diagnostics."""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.deye_local_custom.const import (
    CONF_DEVICE_TYPE,
    DOMAIN,
    HOLD,
    Tier,
)
from custom_components.deye_local_custom.coordinator import platform_of
from custom_components.deye_local_custom.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.deye_local_custom.models.sg03lp1 import SG03LP1
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .conftest import Clock
from .fake_logger import INVERTER_SERIAL, LOGGER_SERIAL, FakeLogger

# The entity ids of the previous integration (StephanJoubert's solarman): kept
PREVIOUS = [
    "sensor.inverter_ac_temperature",
    "sensor.inverter_battery_current",
    "sensor.inverter_battery_power",
    "sensor.inverter_battery_soc",
    "sensor.inverter_battery_voltage",
    "sensor.inverter_dc_temperature",
    "sensor.inverter_grid_connected_status",
    "sensor.inverter_grid_frequency",
    "sensor.inverter_grid_voltage_l1",
    "sensor.inverter_load_frequency",
    "sensor.inverter_load_l1_power",
    "sensor.inverter_load_voltage",
    "sensor.inverter_running_status",
    "sensor.inverter_total_battery_charge",
    "sensor.inverter_total_battery_discharge",
    "sensor.inverter_total_grid_power",
    "sensor.inverter_total_load_consumption",
    "sensor.inverter_total_load_power",
    "sensor.inverter_total_power",
]
NEW = [
    "sensor.inverter_grid_charging_current",
    "select.inverter_grid_charging_current_preset",
    "binary_sensor.inverter_connection",
]


def state(hass: HomeAssistant, entity_id: str) -> str:
    current = hass.states.get(entity_id)
    assert current is not None, entity_id
    return current.state


async def refresh(entry: MockConfigEntry, clock: Clock, seconds: float = 10) -> None:
    clock.advance(seconds)
    await entry.runtime_data.async_refresh()


async def test_setup_and_unload(
    hass: HomeAssistant, entry: MockConfigEntry, logger: FakeLogger
) -> None:
    assert entry.state is ConfigEntryState.LOADED
    coordinator = entry.runtime_data

    (device,) = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    assert device.identifiers == {(DOMAIN, INVERTER_SERIAL)}
    assert (device.manufacturer, device.model, device.name) == (
        "Deye",
        "SUN-5K-SG03LP1-EU",
        "Inverter",
    )

    registry = er.async_get(hass)
    entities = er.async_entries_for_config_entry(registry, entry.entry_id)
    enabled = sorted(e.entity_id for e in entities if not e.disabled)
    assert enabled == sorted(PREVIOUS + NEW)
    assert len(entities) == len(coordinator.model.descriptions) + 1  # + connection

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert coordinator.client.closed


async def test_values(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert state(hass, "sensor.inverter_grid_voltage_l1") == "230.5"
    assert state(hass, "sensor.inverter_grid_connected_status") == "On-Grid"
    assert state(hass, "sensor.inverter_running_status") == "Normal"
    assert state(hass, "sensor.inverter_battery_power") == "-1570"
    assert state(hass, "sensor.inverter_battery_current") == "-29.5"
    assert state(hass, "sensor.inverter_total_battery_charge") == "7456.5"
    assert state(hass, "sensor.inverter_dc_temperature") == "35.0"
    assert state(hass, "sensor.inverter_grid_charging_current") == "50"
    assert state(hass, "select.inverter_grid_charging_current_preset") == "50 A"
    assert state(hass, "binary_sensor.inverter_connection") == STATE_ON


async def test_requests(
    hass: HomeAssistant, entry: MockConfigEntry, logger: FakeLogger, clock: Clock
) -> None:
    # First refresh, before the entities exist: from the registry and the defaults
    assert logger.reads == [(150, 81), (59, 33)]
    logger.requests.clear()
    for _ in range(5):
        await refresh(entry, clock)
    assert logger.reads == [(150, 45)] * 5
    logger.requests.clear()
    await refresh(entry, clock, 11)  # 61 s: the minute's values too
    assert logger.reads == [(150, 81), (59, 33)]
    assert logger.connections == 1


async def test_disabled_entity_is_not_read(
    hass: HomeAssistant, entry: MockConfigEntry, logger: FakeLogger, clock: Clock
) -> None:
    er.async_get(hass).async_update_entity(
        "sensor.inverter_grid_connected_status",
        disabled_by=er.RegistryEntryDisabler.USER,
    )
    await hass.async_block_till_done()
    logger.requests.clear()
    await refresh(entry, clock)
    assert logger.reads == [(150, 43)]  # up to 192


async def test_enabled_entity_is_read_from_the_first_refresh(
    hass: HomeAssistant,
    enable_custom_integrations: None,
    config_entry: MockConfigEntry,
    logger: FakeLogger,
    clock: Clock,
) -> None:
    config_entry.add_to_hass(hass)
    er.async_get(hass).async_get_or_create(
        "sensor",
        DOMAIN,
        f"{INVERTER_SERIAL}_fault_codes",
        config_entry=config_entry,
        suggested_object_id="inverter_fault_codes",
    )
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert logger.reads == [(150, 81), (59, 48)]
    assert state(hass, "sensor.inverter_fault_codes") == "OK"
    await hass.config_entries.async_unload(config_entry.entry_id)


async def test_values_are_kept_through_a_logger_reboot(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    logger: FakeLogger,
    clock: Clock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    fast, slow = "sensor.inverter_grid_voltage_l1", "sensor.inverter_running_status"
    connection = "binary_sensor.inverter_connection"
    await logger.stop()
    for _ in range(10):  # 100 s: the fast values still hold
        await refresh(entry, clock)
    assert state(hass, fast) == "230.5"
    assert state(hass, connection) == STATE_ON
    assert "No answer" not in caplog.text

    await refresh(entry, clock, 1)  # 101 s
    assert state(hass, fast) == STATE_UNAVAILABLE
    assert state(hass, slow) == "Normal"  # 60 + 90 s
    assert state(hass, connection) == "off"
    assert caplog.text.count("No answer from the inverter") == 1
    assert "127.0.0.1" not in caplog.text
    assert str(LOGGER_SERIAL) not in caplog.text

    await refresh(entry, clock, 50)  # 151 s
    assert state(hass, slow) == STATE_UNAVAILABLE

    await logger.start()
    await refresh(entry, clock)
    assert state(hass, fast) == "230.5"
    assert state(hass, slow) == "Normal"
    assert state(hass, connection) == STATE_ON
    assert caplog.text.count("The inverter answers again") == 1


async def test_hold_is_counted_from_each_value_s_last_read(
    hass: HomeAssistant, entry: MockConfigEntry, logger: FakeLogger, clock: Clock
) -> None:
    """The slow block failing alone: retried every poll, kept for 60 + HOLD s."""
    await refresh(entry, clock, 61)
    logger.exception = 2  # every request refused from now on
    for _ in range(int((60 + HOLD) // 10)):
        await refresh(entry, clock)
    assert state(hass, "sensor.inverter_running_status") == "Normal"
    await refresh(entry, clock)
    assert state(hass, "sensor.inverter_running_status") == STATE_UNAVAILABLE


async def test_setup_retried_while_the_logger_is_away(
    hass: HomeAssistant,
    enable_custom_integrations: None,
    config_entry: MockConfigEntry,
    logger: FakeLogger,
    clock: Clock,
) -> None:
    await logger.stop()
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_model_dropped_by_a_later_version(
    hass: HomeAssistant,
    enable_custom_integrations: None,
    entry_data: dict[str, Any],
) -> None:
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=INVERTER_SERIAL,
        data={**entry_data, CONF_DEVICE_TYPE: 0x0006},
    )
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_ERROR


async def test_unknown_value(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    logger: FakeLogger,
    clock: Clock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger.registers[194] = 7
    await refresh(entry, clock)
    await refresh(entry, clock)
    assert state(hass, "sensor.inverter_grid_connected_status") == STATE_UNKNOWN
    assert caplog.text.count("unexpected value [7]") == 1
    logger.registers[194] = 0
    await refresh(entry, clock)
    assert state(hass, "sensor.inverter_grid_connected_status") == "Off-Grid"


async def test_state_icons(
    hass: HomeAssistant, entry: MockConfigEntry, logger: FakeLogger, clock: Clock
) -> None:
    """On-Grid: no icon of its own (icons.json's default); Off-Grid: its own."""
    entity_id = "sensor.inverter_grid_connected_status"
    assert "icon" not in hass.states.get(entity_id).attributes
    entry_ = er.async_get(hass).async_get(entity_id)
    assert entry_.translation_key == "grid_connected_status"
    logger.registers[194] = 0
    await refresh(entry, clock)
    assert hass.states.get(entity_id).attributes["icon"] == "mdi:transmission-tower-off"


async def set_up(hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()


async def test_options_set_the_intervals(
    hass: HomeAssistant,
    enable_custom_integrations: None,
    entry_data: dict[str, Any],
    logger: FakeLogger,
    clock: Clock,
) -> None:
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=INVERTER_SERIAL,
        data=entry_data,
        options={"grid_voltage_interval": 5, "fast_interval": 10, "slow_interval": 60},
    )
    await set_up(hass, config_entry)
    coordinator = config_entry.runtime_data
    assert coordinator.update_interval == timedelta(seconds=5)
    assert logger.reads == [(150, 81), (59, 33)]
    logger.requests.clear()
    for _ in range(4):
        await refresh(config_entry, clock, 5)
    assert logger.reads == [(150, 1), (150, 45), (150, 1), (150, 45)]

    diagnostics = await async_get_config_entry_diagnostics(hass, config_entry)
    assert diagnostics["intervals"] == {"grid_voltage": 5, "fast": 10, "slow": 60}
    assert diagnostics["tick"] == 5
    assert diagnostics["planned_requests_per_minute"] == 13.0
    await hass.config_entries.async_unload(config_entry.entry_id)


@pytest.mark.parametrize("stored", [7, None, "abc"])
async def test_options_not_valid_read_at_the_defaults(
    hass: HomeAssistant,
    enable_custom_integrations: None,
    entry_data: dict[str, Any],
    logger: FakeLogger,
    clock: Clock,
    caplog: pytest.LogCaptureFixture,
    stored: Any,
) -> None:
    """A choice a later version dropped, a value edited by hand: the defaults, one
    warning; the form offers the defaults (its selectors take only their choices)."""
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=INVERTER_SERIAL,
        data=entry_data,
        options={"grid_voltage_interval": stored},
    )
    await set_up(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED
    assert config_entry.runtime_data.model.tick == 10
    assert caplog.text.count("are not valid (grid_voltage: not_a_choice)") == 1

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    assert suggested(result)["grid_voltage_interval"] == "10"
    assert result["description_placeholders"] == {"requests": "7"}
    await hass.config_entries.async_unload(config_entry.entry_id)


def suggested(result: Any) -> dict[str, Any]:
    """The values a form starts with."""
    return {
        str(key.schema): key.description["suggested_value"]
        for key in result["data_schema"].schema
    }


def disable(hass: HomeAssistant, config_entry: MockConfigEntry, keys: set[str]) -> None:
    """Entities disabled by the user before the entry is set up."""
    registry = er.async_get(hass)
    for d in SG03LP1.descriptions:
        if d.key in keys:
            registry.async_get_or_create(
                str(platform_of(d)),
                DOMAIN,
                f"{INVERTER_SERIAL}_{d.key}",
                config_entry=config_entry,
                suggested_object_id=f"inverter_{d.key}",
                disabled_by=er.RegistryEntryDisabler.USER,
            )


async def test_connection_follows_the_entities_read(
    hass: HomeAssistant,
    enable_custom_integrations: None,
    entry_data: dict[str, Any],
    logger: FakeLogger,
    clock: Clock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Grid voltage disabled, fast values every 60 s: polls in between read nothing.
    Connection goes off with the values (60 + HOLD), not a polling step + HOLD."""
    connection, soc = "binary_sensor.inverter_connection", "sensor.inverter_battery_soc"
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=INVERTER_SERIAL,
        data=entry_data,
        options={"grid_voltage_interval": 10, "fast_interval": 60, "slow_interval": 60},
    )
    config_entry.add_to_hass(hass)
    disable(hass, config_entry, {"grid_voltage_l1"})
    await set_up(hass, config_entry)
    logger.requests.clear()
    for _ in range(5):
        await refresh(config_entry, clock)
    assert logger.reads == []  # 10-50 s
    await logger.stop()
    for _ in range(10):  # 60-150 s: every poll fails
        await refresh(config_entry, clock)
        assert (state(hass, connection), state(hass, soc)) == (STATE_ON, "87")
    await refresh(config_entry, clock)  # 160 s
    assert (state(hass, connection), state(hass, soc)) == ("off", STATE_UNAVAILABLE)
    assert caplog.text.count("No answer from the inverter for over 150 s") == 1
    await hass.config_entries.async_unload(config_entry.entry_id)


async def test_connection_stays_on_with_only_slow_entities(
    hass: HomeAssistant,
    enable_custom_integrations: None,
    entry_data: dict[str, Any],
    logger: FakeLogger,
    clock: Clock,
) -> None:
    """Slow values every 120 s and nothing faster enabled: no off between reads."""
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=INVERTER_SERIAL,
        data=entry_data,
        options={
            "grid_voltage_interval": 10,
            "fast_interval": 10,
            "slow_interval": 120,
        },
    )
    config_entry.add_to_hass(hass)
    faster = {d.key for d in SG03LP1.descriptions if d.tier is not Tier.SLOW}
    disable(hass, config_entry, faster)
    await set_up(hass, config_entry)
    for _ in range(24):  # 240 s
        await refresh(config_entry, clock)
        assert state(hass, "binary_sensor.inverter_connection") == STATE_ON
    assert config_entry.runtime_data.requests == {"ok": 6, "failed": 0}
    await hass.config_entries.async_unload(config_entry.entry_id)


async def test_changing_the_options_reloads(
    hass: HomeAssistant, entry: MockConfigEntry, logger: FakeLogger
) -> None:
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["description_placeholders"] == {"requests": "7"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"grid_voltage_interval": "5", "fast_interval": "10", "slow_interval": "60"},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options == {
        "grid_voltage_interval": 5,
        "fast_interval": 10,
        "slow_interval": 60,
    }
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.model.tick == 5
    assert state(hass, "sensor.inverter_grid_voltage_l1") == "230.5"

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["description_placeholders"] == {"requests": "13"}


async def test_saving_the_same_intervals_does_not_reload(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """The defaults saved over no options yet (just added, or updated from 0.1.0)."""
    coordinator = entry.runtime_data
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"grid_voltage_interval": "10", "fast_interval": "10", "slow_interval": "60"},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options == {}
    assert entry.runtime_data is coordinator


async def test_diagnostics_hide_address_and_serials(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["entry"]["host"] == "**REDACTED**"
    assert diagnostics["entry"]["logger_serial"] == "**REDACTED**"
    assert diagnostics["options"] == {}
    assert diagnostics["intervals"] == {"grid_voltage": 10, "fast": 10, "slow": 60}
    assert diagnostics["planned_requests_per_minute"] == 7.0
    assert diagnostics["model"] == "sg03lp1"
    assert diagnostics["values"]["grid_voltage_l1"]["value"] == 230.5
    assert diagnostics["packages"]["pysolarmanv5"]["version"] == "3.0.6"
    assert set(diagnostics["packages"]) == {"pysolarmanv5", "umodbus", "pyserial"}
    assert INVERTER_SERIAL not in str(diagnostics)
