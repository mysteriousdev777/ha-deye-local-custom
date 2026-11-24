"""Sensors of the model."""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType

from .coordinator import DeyeConfigEntry
from .description import DeyeSensorDescription
from .entity import DeyeEntity

PARALLEL_UPDATES = 0  # the coordinator polls


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DeyeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(DeyeSensor(coordinator, d) for d in coordinator.model.sensors)


class DeyeSensor(DeyeEntity, SensorEntity):
    entity_description: DeyeSensorDescription

    @property
    def native_value(self) -> StateType:
        return self.value

    @property
    def icon(self) -> str | None:
        """The state's own icon; else none, and the default one of icons.json."""
        icons = self.entity_description.state_icons
        return icons.get(self.value) if icons else None
