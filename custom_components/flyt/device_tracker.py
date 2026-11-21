"""Map position per vehicle: where its tag was last read (toll station or ferry quay)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.device_tracker import SourceType, TrackerEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import FlytCoordinator, Obe, Passing


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: FlytCoordinator = entry.runtime_data
    async_add_entities(
        FlytPositionTracker(coordinator, obe.obe_number)
        for obe in coordinator.data.obes.values()
        if obe.status == "Active"
    )


class FlytPositionTracker(CoordinatorEntity[FlytCoordinator], TrackerEntity):
    """Position of the last toll station or ferry quay the tag passed."""

    _attr_has_entity_name = True
    _attr_translation_key = "last_position"
    _attr_icon = "mdi:map-marker-path"

    def __init__(self, coordinator: FlytCoordinator, obe_number: str) -> None:
        super().__init__(coordinator)
        self._obe_number = obe_number
        self._attr_unique_id = f"{obe_number}_last_position"
        # Same device as the sensors (created there with name and model)
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, obe_number)})

    @property
    def _passing(self) -> Passing | None:
        obe: Obe | None = self.coordinator.data.obes.get(self._obe_number)
        return obe.last_located_passing if obe else None

    @property
    def source_type(self) -> SourceType:
        return SourceType.GPS

    @property
    def latitude(self) -> float | None:
        p = self._passing
        return p.latitude if p else None

    @property
    def longitude(self) -> float | None:
        p = self._passing
        return p.longitude if p else None

    @property
    def location_accuracy(self) -> float:
        return 100

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        p = self._passing
        if p is None:
            return None
        return {
            "station": p.station,
            "nvdb_name": p.location_name,
            "kind": p.kind,
            "time": p.time.isoformat(),
            "price": p.price,
            "operator": p.operator,
        }
