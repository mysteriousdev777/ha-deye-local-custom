"""Binary sensors of the model, and the connection to the inverter."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import DeyeConfigEntry, DeyeCoordinator
from .description import DeyeBinarySensorDescription
from .entity import DeyeEntity

PARALLEL_UPDATES = 0  # the coordinator polls


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DeyeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        [
            DeyeConnectionSensor(coordinator),
            *(
                DeyeBinarySensor(coordinator, d)
                for d in coordinator.model.binary_sensors
            ),
        ]
    )


class DeyeBinarySensor(DeyeEntity, BinarySensorEntity):
    entity_description: DeyeBinarySensorDescription

    @property
    def is_on(self) -> bool | None:
        return self.value


class DeyeConnectionSensor(CoordinatorEntity[DeyeCoordinator], BinarySensorEntity):
    """On while the inverter's values are fresh; always available, for alerts.

    Goes off when the fast values go unavailable (no good read for a polling step +
    HOLD): not on the logger's planned reboots.
    """

    _attr_has_entity_name = True
    _attr_name = "Connection"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: DeyeCoordinator) -> None:
        super().__init__(coordinator)  # no context: reads no registers
        self._attr_unique_id = f"{coordinator.serial}_connection"
        self._attr_device_info = coordinator.device_info

    @property
    def available(self) -> bool:
        return True

    @property
    def is_on(self) -> bool:
        return self.coordinator.connected
