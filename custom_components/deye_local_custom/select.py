"""The writable registers: each a select with the model's fixed values only."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import DeyeConfigEntry, DeyeCoordinator
from .description import DeyeSelectDescription
from .entity import DeyeEntity

PARALLEL_UPDATES = 0  # the coordinator's lock orders the requests


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DeyeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(DeyeSelect(coordinator, d) for d in coordinator.model.selects)


class DeyeSelect(DeyeEntity, SelectEntity):
    """The register's value as an option; none (unknown) for a value not in the list,
    such as one set on the inverter's screen."""

    entity_description: DeyeSelectDescription

    def __init__(
        self, coordinator: DeyeCoordinator, description: DeyeSelectDescription
    ) -> None:
        super().__init__(coordinator, description)
        self._attr_options = list(description.options or ())

    @property
    def current_option(self) -> str | None:
        d = self.entity_description
        if self.value in d.values:
            return self._attr_options[d.values.index(self.value)]
        return None

    async def async_select_option(self, option: str) -> None:
        """Write it and wait until the inverter confirms it: attempts start until
        WRITE_DEADLINE, and the last one may run past it."""
        d = self.entity_description
        value = d.values[self._attr_options.index(option)]
        await self.coordinator.async_write(d.registers[0], value)
