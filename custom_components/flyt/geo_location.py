"""Map points for your passings: one per vehicle and station/quay it passed.

Use `geo_location_sources: [flyt]` in the map card. Covers the last ~45 days.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.components.geo_location import GeolocationEvent
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfLength
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util.location import distance

from .const import CONF_SHOW_ALL_PASSINGS, GEO_SOURCE
from .coordinator import FlytCoordinator


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: FlytCoordinator = entry.runtime_data
    manager = _GeoManager(hass, entry, coordinator, async_add_entities)
    manager.update()
    entry.async_on_unload(coordinator.async_add_listener(manager.update))


class FlytGeoPoint(GeolocationEvent):
    """A point on the map with a fixed position; name and attributes may change."""

    _attr_should_poll = False
    _attr_unit_of_measurement = UnitOfLength.KILOMETERS

    def __init__(
        self,
        source: str,
        name: str,
        icon: str,
        lat: float,
        lon: float,
        dist_km: float | None,
        attrs: dict[str, Any],
    ) -> None:
        self._source = source
        self._attr_name = name
        self._attr_icon = icon
        self._attr_latitude = lat
        self._attr_longitude = lon
        self._attr_distance = dist_km
        self._attrs = attrs

    @property
    def source(self) -> str:
        return self._source

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self._attrs

    @callback
    def refresh(self, name: str, attrs: dict[str, Any]) -> None:
        if attrs != self._attrs or name != self._attr_name:
            self._attrs = attrs
            self._attr_name = name
            if self.hass:
                self.async_write_ha_state()


# key -> (factory for a new point, name, attributes)
Wanted = dict[str, tuple[Callable[[], FlytGeoPoint], str, dict[str, Any]]]


class _GeoManager:
    """Adds, updates and removes points as data and switches change."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        coordinator: FlytCoordinator,
        add: AddEntitiesCallback,
    ) -> None:
        self.hass = hass
        self.opts = entry.options
        self.coordinator = coordinator
        self.add = add
        self.points: dict[str, FlytGeoPoint] = {}

    def _km(self, lat: float, lon: float) -> float | None:
        meters = distance(
            self.hass.config.latitude, self.hass.config.longitude, lat, lon
        )
        return round(meters / 1000, 1) if meters is not None else None

    @callback
    def update(self) -> None:
        wanted: Wanted = {}
        if self.opts.get(CONF_SHOW_ALL_PASSINGS):
            self._passing_points(wanted)

        new: list[FlytGeoPoint] = []
        for key, (factory, name, attrs) in wanted.items():
            if key in self.points:
                self.points[key].refresh(name, attrs)
            else:
                self.points[key] = point = factory()
                new.append(point)
        for key in [k for k in self.points if k not in wanted]:
            point = self.points.pop(key)
            if point.hass:
                self.hass.async_create_task(point.async_remove(force_remove=True))
        if new:
            self.add(new)

    def _passing_points(self, wanted: Wanted) -> None:
        for key, sp in self.coordinator.station_points().items():
            attrs = {
                "registration_number": sp.registration_number,
                "vehicle": sp.vehicle_name,
                "station": sp.station,
                "kind": sp.kind,
                "count": sp.count,
                "total": sp.total,
                "currency": sp.currency,
                "last_passing": sp.last_time.isoformat(),
            }
            # Shown as the marker's hover text: station · car · last passing
            name = (
                f"{sp.name} · {sp.registration_number or sp.obe_number}"
                f" · {sp.last_time.strftime('%d.%m %H:%M')}"
            )
            icon = "mdi:ferry" if sp.kind == "ferry" else "mdi:boom-gate"
            dist = self._km(sp.latitude, sp.longitude)
            wanted[f"p|{key}"] = (
                lambda n=name, i=icon, a=attrs, s=sp, d=dist: FlytGeoPoint(
                    GEO_SOURCE, n, i, s.latitude, s.longitude, d, a
                ),
                name,
                attrs,
            )
