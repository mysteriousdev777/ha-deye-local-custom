"""Writing a grid charging current preset: confirmed by reading, retried, refused."""

from __future__ import annotations

import asyncio

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.deye_local_custom import coordinator
from homeassistant.components.select import (
    DOMAIN as SELECT_DOMAIN,
    SERVICE_SELECT_OPTION,
)
from homeassistant.const import ATTR_ENTITY_ID, ATTR_OPTION, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from .conftest import Clock
from .fake_logger import FakeLogger

SELECT = "select.inverter_grid_charging_current_preset"
SENSOR = "sensor.inverter_grid_charging_current"


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(coordinator, "WRITE_RETRY", 0.05)


async def select(hass: HomeAssistant, option: str) -> None:
    await hass.services.async_call(
        SELECT_DOMAIN,
        SERVICE_SELECT_OPTION,
        {ATTR_ENTITY_ID: SELECT, ATTR_OPTION: option},
        blocking=True,
    )


async def test_options(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    current = hass.states.get(SELECT)
    assert current is not None
    assert current.attributes["options"] == ["30 A", "50 A"]
    assert current.state == "50 A"


async def test_write_and_confirm(
    hass: HomeAssistant, entry: MockConfigEntry, logger: FakeLogger
) -> None:
    logger.requests.clear()
    await select(hass, "30 A")
    assert logger.requests == [
        ("read", 230, 1),
        ("write", 230, [30]),
        ("read", 230, 1),
    ]
    assert logger.registers[230] == 30
    assert hass.states.get(SELECT).state == "30 A"
    assert hass.states.get(SENSOR).state == "30"


async def test_same_value_is_not_written(
    hass: HomeAssistant, entry: MockConfigEntry, logger: FakeLogger
) -> None:
    logger.requests.clear()
    await select(hass, "50 A")
    assert logger.requests == [("read", 230, 1)]


async def test_retried_while_the_logger_does_not_answer(
    hass: HomeAssistant, entry: MockConfigEntry, logger: FakeLogger
) -> None:
    logger.requests.clear()
    logger.lose = {0, 2}  # the first read, then the write: their replies are lost
    await select(hass, "30 A")
    assert logger.registers[230] == 30
    assert logger.requests == [
        ("read", 230, 1),  # lost
        ("read", 230, 1),
        ("write", 230, [30]),  # went through, reply lost
        ("read", 230, 1),  # next attempt: already 30, nothing to write
    ]
    assert hass.states.get(SELECT).state == "30 A"


async def test_gives_up_at_the_deadline(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    logger: FakeLogger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(coordinator, "WRITE_DEADLINE", 1.0)
    logger.silent = True
    with pytest.raises(HomeAssistantError, match="did not confirm"):
        await select(hass, "30 A")
    assert logger.registers[230] == 50


async def test_last_attempt_by_the_deadline(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    logger: FakeLogger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure with less than WRITE_RETRY left: one more attempt at the deadline."""
    monkeypatch.setattr(coordinator, "WRITE_DEADLINE", 1.0)
    monkeypatch.setattr(coordinator, "WRITE_RETRY", 0.8)
    logger.requests.clear()
    logger.lose = {0}  # the first read times out (0.3 s): 0.7 s left
    await select(hass, "30 A")
    assert logger.registers[230] == 30
    assert [kind for kind, *_ in logger.requests] == ["read", "read", "write", "read"]


async def test_polls_wait_while_a_write_goes_on(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    logger: FakeLogger,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A poll in a write's pause sends nothing: the write's attempts do not queue
    behind polls that wait for a silent logger."""
    monkeypatch.setattr(coordinator, "WRITE_RETRY", 1.0)
    deye = entry.runtime_data
    logger.requests.clear()
    logger.received.clear()
    logger.lose = {0}
    write = asyncio.create_task(select(hass, "30 A"))
    await logger.received.wait()  # the first attempt's read, its reply lost
    async with deye.lock:  # free again once that attempt failed: the write pauses
        pass
    clock.advance(10)
    await deye.async_refresh()
    assert logger.requests == [("read", 230, 1)]
    await write
    assert [kind for kind, *_ in logger.requests] == ["read", "read", "write", "read"]
    clock.advance(10)
    await deye.async_refresh()
    assert logger.reads[-1] == (150, 45)  # polls go on after the write


async def test_interrupted_by_a_reload(
    hass: HomeAssistant, entry: MockConfigEntry, logger: FakeLogger
) -> None:
    """Unloading the entry ends a write at once, and says so (not "tried for 60 s")."""
    logger.silent = True
    logger.received.clear()
    write = asyncio.create_task(select(hass, "30 A"))
    await logger.received.wait()  # the first read waits for its reply
    assert await hass.config_entries.async_unload(entry.entry_id)
    with pytest.raises(HomeAssistantError, match="integration was reloaded"):
        await write


async def test_value_not_taken(
    hass: HomeAssistant, entry: MockConfigEntry, logger: FakeLogger
) -> None:
    logger.ignore_writes = True
    with pytest.raises(HomeAssistantError, match="did not take 30: it reads 50"):
        await select(hass, "30 A")
    assert hass.states.get(SELECT).state == "50 A"


async def test_refused(
    hass: HomeAssistant, entry: MockConfigEntry, logger: FakeLogger
) -> None:
    logger.exception = 3  # illegal data value
    with pytest.raises(HomeAssistantError, match="refused"):
        await select(hass, "30 A")
    assert ("write", 230, [30]) not in logger.requests


async def test_only_listed_options(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    with pytest.raises(ServiceValidationError):
        await select(hass, "40 A")


async def test_coordinator_refuses_other_values(entry: MockConfigEntry) -> None:
    with pytest.raises(ServiceValidationError):
        await entry.runtime_data.async_write(230, 40)
    with pytest.raises(ServiceValidationError):
        await entry.runtime_data.async_write(210, 50)


async def test_value_set_on_the_inverter(
    hass: HomeAssistant, entry: MockConfigEntry, logger: FakeLogger, clock: Clock
) -> None:
    logger.registers[230] = 40
    clock.advance(61)
    await entry.runtime_data.async_refresh()
    assert hass.states.get(SELECT).state == STATE_UNKNOWN
    assert hass.states.get(SENSOR).state == "40"


async def test_write_and_poll_never_overlap(
    hass: HomeAssistant, entry: MockConfigEntry, logger: FakeLogger, clock: Clock
) -> None:
    logger.delay = 0.05
    clock.advance(61)
    await asyncio.gather(select(hass, "30 A"), entry.runtime_data.async_refresh())
    assert logger.max_in_flight == 1
    assert logger.registers[230] == 30
