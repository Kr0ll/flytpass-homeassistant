"""Map points: your passings per station, and (optionally) all toll stations.

Two sources for the map card's `geo_location_sources`:
- "flyt": one point per vehicle and station/quay it passed (last ~45 days)
- "flyt_stations": every NVDB toll station within a radius, with tariffs
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

from .const import (
    CONF_EV_SHARE,
    CONF_SHOW_ALL_PASSINGS,
    CONF_SHOW_ESTIMATES,
    CONF_SHOW_STATIONS,
    CONF_STATION_RADIUS,
    CONF_TAG_DISCOUNT,
    DEFAULT_EV_SHARE_PCT,
    DEFAULT_STATION_RADIUS_KM,
    DEFAULT_TAG_DISCOUNT_PCT,
    GEO_SOURCE,
    GEO_SOURCE_STATIONS,
)
from .coordinator import FlytCoordinator
from .locations import estimate_price, toll_stations

DISCLAIMER = (
    "Takster fra NVDB (Statens vegvesen), fullpris før rabatter. De kan ligge "
    "etter når priser endres. Estimert pris er regnet med satsene du har valgt. "
    "Flyt og bompengeselskapene gir ikke nødvendigvis rabatt på alle stasjoner, "
    "og timesregel og rushtid påvirker hva du betaler. Fasiten er fakturaen."
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: FlytCoordinator = entry.runtime_data
    manager = _GeoManager(hass, entry, coordinator, async_add_entities)
    manager.update()
    entry.async_on_unload(coordinator.async_add_listener(manager.update))


class FlytGeoPoint(GeolocationEvent):
    """A point on the map with a fixed position and changing attributes."""

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
    def refresh(self, attrs: dict[str, Any]) -> None:
        if attrs != self._attrs:
            self._attrs = attrs
            if self.hass:
                self.async_write_ha_state()


Wanted = dict[str, tuple[Callable[[], FlytGeoPoint], dict[str, Any]]]


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
        if self.opts.get(CONF_SHOW_STATIONS):
            self._station_points(wanted)

        new: list[FlytGeoPoint] = []
        for key, (factory, attrs) in wanted.items():
            if key in self.points:
                self.points[key].refresh(attrs)
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
            name = f"{sp.name} · {sp.registration_number or sp.obe_number}"
            icon = "mdi:ferry" if sp.kind == "ferry" else "mdi:boom-gate"
            dist = self._km(sp.latitude, sp.longitude)
            wanted[f"p|{key}"] = (
                lambda n=name, i=icon, a=attrs, s=sp, d=dist: FlytGeoPoint(
                    GEO_SOURCE, n, i, s.latitude, s.longitude, d, a
                ),
                attrs,
            )

    def _station_points(self, wanted: Wanted) -> None:
        radius = float(self.opts.get(CONF_STATION_RADIUS, DEFAULT_STATION_RADIUS_KM))
        estimates = self.opts.get(CONF_SHOW_ESTIMATES)
        tag = float(self.opts.get(CONF_TAG_DISCOUNT, DEFAULT_TAG_DISCOUNT_PCT))
        ev = float(self.opts.get(CONF_EV_SHARE, DEFAULT_EV_SHARE_PCT))
        data = self.coordinator.data
        cars = [o for o in (data.obes.values() if data else []) if o.status == "Active"]

        for lat, lon, name, info in toll_stations(self.coordinator.locations_index):
            dist = self._km(lat, lon)
            if dist is None or dist > radius:
                continue
            attrs: dict[str, Any] = {k: v for k, v in info.items() if v is not None}
            attrs["price"] = info.get("price_small")  # handy for label_mode
            if estimates:
                attrs["estimates"] = [
                    {
                        "vehicle": o.registration_number or o.obe_number,
                        "price": estimate_price(
                            info.get("price_large" if o.large else "price_small"),
                            o.electric,
                            tag,
                            ev,
                        ),
                        "rush_price": estimate_price(
                            info.get(
                                "rush_price_large" if o.large else "rush_price_small"
                            ),
                            o.electric,
                            tag,
                            ev,
                        ),
                    }
                    for o in cars
                ]
                attrs["estimate_basis"] = (
                    f"Brikkerabatt {tag:g} %, elbil {ev:g} % av fullpris"
                )
            attrs["disclaimer"] = DISCLAIMER
            wanted[f"s|{lat:.5f},{lon:.5f}|{name}"] = (
                lambda n=name, a=attrs, la=lat, lo=lon, d=dist: FlytGeoPoint(
                    GEO_SOURCE_STATIONS, n, "mdi:cash-marker", la, lo, d, a
                ),
                attrs,
            )
