"""Polls the inverter, keeps values through short gaps, writes the presets.

Each entity has its own reading interval and keeps its value for that interval + HOLD
after its last good read: the logger's reboot every 3 hours (30-45 s away) does not make
anything unavailable, a real loss does, about a minute and a half later. So a failed poll
is not an UpdateFailed (Home Assistant would stop updating the entities after the second
one in a row, and their timers would never run out): the coordinator returns the values
it holds, and each entity works out its availability on every poll.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import timedelta
import logging
from time import monotonic
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import DeyeClient, DeyeConnectionError, DeyeModbusError
from .const import DOMAIN, HOLD, WRITE_DEADLINE, WRITE_RETRY
from .description import (
    AnyDescription,
    DeyeBinarySensorDescription,
    DeyeSelectDescription,
    Model,
)
from .planner import Request, plan

_LOGGER = logging.getLogger(__name__)

type DeyeConfigEntry = ConfigEntry[DeyeCoordinator]


def platform_of(description: AnyDescription) -> Platform:
    """The entity platform of a description, as the entity registry knows it."""
    if isinstance(description, DeyeSelectDescription):
        return Platform.SELECT
    if isinstance(description, DeyeBinarySensorDescription):
        return Platform.BINARY_SENSOR
    return Platform.SENSOR


class DeyeCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Decoded values by entity key, and when each was last read."""

    config_entry: DeyeConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: DeyeConfigEntry,
        client: DeyeClient,
        model: Model,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=entry.title,
            update_interval=timedelta(seconds=model.tick),
        )
        self.client = client
        self.model = model
        self.serial: str = entry.unique_id or entry.entry_id
        self.values: dict[str, Any] = {}
        self.read_at: dict[str, float] = {}  # monotonic time of each key's last read
        self.last_read: float | None = None  # of any request
        self.last_error: str | None = None
        self.requests: dict[str, int] = {"ok": 0, "failed": 0}
        # One request at a time on the connection; a write holds it for a whole attempt
        # (read, write, read back), not for the pauses between attempts.
        self.lock = asyncio.Lock()
        # Writes going on: polls send nothing meanwhile, so that a write's attempts do
        # not queue behind polls that wait for a silent logger
        self._writes = 0
        self._connected = True  # set up after a good first read
        self._unknown: set[str] = set()  # keys whose unknown value was logged
        self.device_info = DeviceInfo(
            identifiers={(DOMAIN, self.serial)},
            manufacturer=model.manufacturer,
            model=model.name,
            name=model.device_name,
            serial_number=self.serial,
        )

    # Availability

    def is_fresh(self, description: AnyDescription) -> bool:
        """Read within its interval + HOLD."""
        last = self.read_at.get(description.key)
        return last is not None and monotonic() - last <= description.interval + HOLD

    @property
    def connected(self) -> bool:
        """A good read within one polling step + HOLD: off when the fast values go."""
        return (
            self.last_read is not None
            and monotonic() - self.last_read <= self.model.tick + HOLD
        )

    # Polling

    def active_keys(self) -> set[str]:
        """Keys of the entities to read: those that exist and are enabled.

        Disabling an entity removes it (and its listener) at once; enabling one reloads
        the entry. Before the entities exist (first refresh), the entity registry and
        the descriptions' defaults tell.
        """
        if keys := set(self.async_contexts()):
            return keys
        registry = er.async_get(self.hass)
        keys = set()
        for d in self.model.descriptions:
            entity_id = registry.async_get_entity_id(
                platform_of(d), DOMAIN, f"{self.serial}_{d.key}"
            )
            entry = registry.async_get(entity_id) if entity_id else None
            enabled = not entry.disabled if entry else d.entity_registry_enabled_default
            if enabled:
                keys.add(d.key)
        return keys

    async def _async_update_data(self) -> dict[str, Any]:
        active = self.active_keys()
        requests = plan(self.model, active, self.read_at, monotonic())
        for request in requests:
            if self._writes:  # the write goes first; what is due stays due
                break
            try:
                async with self.lock:
                    words = await self.client.read(request.first, request.count)
            except DeyeModbusError as err:  # this range only: the others may work
                self._failed(request, err)
                continue
            except DeyeConnectionError as err:  # the rest would wait the same timeout
                self._failed(request, err)
                break
            self.requests["ok"] += 1
            self._store(request, words, active)
        if self.data is None and requests and self.last_read is None:
            # First refresh: setup is retried later (ConfigEntryNotReady)
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="no_answer",
                translation_placeholders={"reason": self.last_error or "-"},
            )
        if requests:  # with nothing to read, nothing failed
            self._log_connection()
        return self.values

    def _failed(self, request: Request, err: Exception) -> None:
        self.requests["failed"] += 1
        self.last_error = str(err)
        _LOGGER.debug(
            "Registers %s-%s: %s (%s)",
            request.first,
            request.last,
            err,
            type(err).__name__,
        )

    def _store(self, request: Request, words: Sequence[int], active: set[str]) -> None:
        """Decode every active entity the reply covers, due or not."""
        now = monotonic()
        for d in self.model.descriptions:
            if d.key not in active or not request.holds(d):
                continue
            raw = [words[r - request.first] for r in d.registers]
            value = d.decode(raw)
            if value is None:
                if d.key not in self._unknown:
                    self._unknown.add(d.key)
                    _LOGGER.warning(
                        "%s: unexpected value %s in register(s) %s, shown as unknown",
                        d.name,
                        raw,
                        ", ".join(map(str, d.registers)),
                    )
            else:
                self._unknown.discard(d.key)
            self.values[d.key] = value
            self.read_at[d.key] = now
        self.last_read = now

    def _log_connection(self) -> None:
        """Once when the values go unavailable, once when they are back."""
        connected = self.connected
        if connected == self._connected:
            return
        self._connected = connected
        if connected:
            _LOGGER.info("The inverter answers again")
        else:
            _LOGGER.warning(
                "No answer from the inverter for over %s s, its values are unavailable: %s",
                int(self.model.tick + HOLD),
                self.last_error,
            )

    # Writing

    async def async_write(self, register: int, value: int) -> None:
        """Set a register to one of its declared values and confirm it by reading.

        Each attempt reads first (nothing to write if it holds the value already),
        writes, and reads back: the state comes from the inverter, never assumed.
        While the logger does not answer, the next attempt comes WRITE_RETRY seconds
        after a failed one, the last one by WRITE_DEADLINE; polls send nothing
        meanwhile. A refusal or a different value read back ends it at once.
        """
        if value not in self.model.writable.get(register, ()):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="not_writable",
                translation_placeholders={"value": str(value)},
            )
        self._writes += 1
        try:
            await self._async_write(register, value)
        finally:
            self._writes -= 1

    async def _async_write(self, register: int, value: int) -> None:
        loop = self.hass.loop
        deadline = loop.time() + WRITE_DEADLINE
        while True:
            try:
                async with self.lock:
                    current = (await self.client.read(register, 1))[0]
                    if current != value:
                        await self.client.write(register, value)
                        current = (await self.client.read(register, 1))[0]
            except DeyeModbusError as err:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="write_refused",
                    translation_placeholders={"reason": str(err)},
                ) from err
            except DeyeConnectionError as err:
                self.last_error = str(err)
                if self.client.closed:  # the entry was unloaded meanwhile (reload)
                    raise HomeAssistantError(
                        translation_domain=DOMAIN,
                        translation_key="write_interrupted",
                    ) from err
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise HomeAssistantError(
                        translation_domain=DOMAIN,
                        translation_key="write_no_answer",
                        translation_placeholders={
                            "seconds": str(int(WRITE_DEADLINE)),
                            "reason": str(err),
                        },
                    ) from err
                _LOGGER.debug("Writing register %s: %s, trying again", register, err)
                await asyncio.sleep(min(WRITE_RETRY, remaining))
                continue
            self._store(Request(register, 1), [current], self.active_keys())
            self.async_update_listeners()
            if current != value:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="write_not_applied",
                    translation_placeholders={
                        "value": str(value),
                        "current": str(current),
                    },
                )
            _LOGGER.debug("Register %s is %s", register, value)
            return
