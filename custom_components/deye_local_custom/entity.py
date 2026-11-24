"""Base entity: one inverter, one coordinator, a value kept while it is fresh."""

from __future__ import annotations

from typing import Any

from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import DeyeCoordinator
from .description import AnyDescription


class DeyeEntity(CoordinatorEntity[DeyeCoordinator]):
    """An entity read from registers; its key makes its unique id and its context.

    The context tells the coordinator which entities exist: disabled ones are not read.
    """

    _attr_has_entity_name = True
    entity_description: AnyDescription

    def __init__(
        self, coordinator: DeyeCoordinator, description: AnyDescription
    ) -> None:
        super().__init__(coordinator, context=description.key)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.serial}_{description.key}"
        self._attr_device_info = coordinator.device_info

    @property
    def available(self) -> bool:
        """Read within its interval + HOLD, whatever the last poll did."""
        return self.coordinator.is_fresh(self.entity_description)

    @property
    def value(self) -> Any:
        return self.coordinator.values.get(self.entity_description.key)
