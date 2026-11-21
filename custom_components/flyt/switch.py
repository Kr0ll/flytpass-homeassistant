"""Per-vehicle switch: show the vehicle on the map or not."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import FlytCoordinator


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: FlytCoordinator = entry.runtime_data
    async_add_entities(
        FlytShowOnMapSwitch(coordinator, obe.obe_number)
        for obe in coordinator.data.obes.values()
        if obe.status == "Active"
    )


class FlytShowOnMapSwitch(CoordinatorEntity[FlytCoordinator], SwitchEntity, RestoreEntity):
    """Controls whether the vehicle's position and passings show on maps."""

    _attr_has_entity_name = True
    _attr_translation_key = "show_on_map"
    _attr_icon = "mdi:map-check"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: FlytCoordinator, obe_number: str) -> None:
        super().__init__(coordinator)
        self._obe_number = obe_number
        self._attr_unique_id = f"{obe_number}_show_on_map"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, obe_number)})

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in ("on", "off"):
            self.coordinator.set_on_map(self._obe_number, last.state == "on")

    @property
    def is_on(self) -> bool:
        return self.coordinator.is_on_map(self._obe_number)

    async def async_turn_on(self, **kwargs: Any) -> None:
        self.coordinator.set_on_map(self._obe_number, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self.coordinator.set_on_map(self._obe_number, False)
