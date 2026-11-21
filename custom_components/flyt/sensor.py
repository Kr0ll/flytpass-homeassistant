"""Sensors for Flyt bombrikker."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN, TIMEZONE
from .coordinator import Agreement, FlytCoordinator, Obe

RECENT_PASSINGS_ATTR = 10


def _now():
    return dt_util.now(dt_util.get_time_zone(TIMEZONE))


@dataclass(frozen=True, kw_only=True)
class ObeSensorDescription(SensorEntityDescription):
    value_fn: Callable[[Obe], Any]
    attrs_fn: Callable[[Obe], dict[str, Any]] | None = None
    monthly: bool = False


def _last_attrs(obe: Obe) -> dict[str, Any]:
    p = obe.last_passing
    attrs: dict[str, Any] = {
        "recent_passings": [x.as_dict() for x in obe.passings[:RECENT_PASSINGS_ATTR]]
    }
    if p:
        attrs.update(
            station=p.station,
            operator=p.operator,
            price=p.price,
            currency=p.currency,
            license_plate=p.license_plate,
            invoiced=p.invoiced,
        )
    return attrs


def _month_attrs(obe: Obe) -> dict[str, Any]:
    """This month's passings, newest first, for tables on the dashboard."""
    return {
        "passings": [
            {
                "time": p.time.isoformat(),
                "when": p.time.strftime("%d.%m %H:%M"),
                "station": p.station,
                "operator": p.operator,
                "price": p.price,
                "invoiced": p.invoiced,
                "kind": p.kind,
                "latitude": p.latitude,
                "longitude": p.longitude,
            }
            for p in obe.month_passings(_now())
        ]
    }


def _month_cost_attrs(obe: Obe) -> dict[str, Any]:
    month = obe.month_passings(_now())
    return {
        "toll": round(sum(p.price for p in month if p.kind == "toll"), 2),
        "ferry": round(sum(p.price for p in month if p.kind == "ferry"), 2),
    }


OBE_SENSORS: tuple[ObeSensorDescription, ...] = (
    ObeSensorDescription(
        key="last_passing",
        translation_key="last_passing",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda o: o.last_passing.time if o.last_passing else None,
        attrs_fn=_last_attrs,
    ),
    ObeSensorDescription(
        key="last_station",
        translation_key="last_station",
        icon="mdi:boom-gate",
        value_fn=lambda o: o.last_passing.station if o.last_passing else None,
    ),
    ObeSensorDescription(
        key="last_price",
        translation_key="last_price",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="NOK",
        suggested_display_precision=2,
        value_fn=lambda o: o.last_passing.price if o.last_passing else None,
    ),
    ObeSensorDescription(
        key="month_cost",
        monthly=True,
        translation_key="month_cost",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement="NOK",
        suggested_display_precision=2,
        value_fn=lambda o: round(sum(p.price for p in o.month_passings(_now())), 2),
        attrs_fn=_month_cost_attrs,
    ),
    ObeSensorDescription(
        key="month_count",
        monthly=True,
        translation_key="month_count",
        icon="mdi:counter",
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement="passeringer",
        value_fn=lambda o: len(o.month_passings(_now())),
        attrs_fn=_month_attrs,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: FlytCoordinator = entry.runtime_data
    entities: list[SensorEntity] = []
    for obe in coordinator.data.obes.values():
        if obe.status != "Active":
            continue
        entities.extend(
            FlytObeSensor(coordinator, obe.obe_number, d) for d in OBE_SENSORS
        )
    for agr in coordinator.data.agreements.values():
        if agr.status == "Active":
            entities.append(FlytBalanceSensor(coordinator, agr.id))
    async_add_entities(entities)


class FlytObeSensor(CoordinatorEntity[FlytCoordinator], SensorEntity):
    """Sensor tied to one bombrikke."""

    _attr_has_entity_name = True
    # The passing lists can be large and change on every poll: keep them out
    # of the recorder database (they still show in the UI and templates).
    _unrecorded_attributes = frozenset({"recent_passings", "passings"})
    entity_description: ObeSensorDescription

    def __init__(
        self,
        coordinator: FlytCoordinator,
        obe_number: str,
        description: ObeSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._obe_number = obe_number
        self._attr_unique_id = f"{obe_number}_{description.key}"
        obe = coordinator.data.obes[obe_number]
        name = obe.registration_number or obe_number
        if obe.vehicle_name:
            name = f"{name} ({obe.vehicle_name})"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, obe_number)},
            name=name,
            manufacturer="Flyt",
            model="Bombrikke",
            serial_number=obe_number,
        )

    @property
    def _obe(self) -> Obe | None:
        return self.coordinator.data.obes.get(self._obe_number)

    @property
    def available(self) -> bool:
        return super().available and self._obe is not None

    @property
    def native_value(self) -> Any:
        obe = self._obe
        return self.entity_description.value_fn(obe) if obe else None

    @property
    def last_reset(self):
        if not self.entity_description.monthly:
            return None
        return _now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        obe = self._obe
        if obe is None:
            return None
        attrs = {
            "obe_number": obe.obe_number,
            "registration_number": obe.registration_number,
            "customer": obe.customer_name,
            "agreement_number": obe.agreement_number,
            "battery": obe.battery,
        }
        if self.entity_description.attrs_fn:
            attrs.update(self.entity_description.attrs_fn(obe))
        return attrs


class FlytBalanceSensor(CoordinatorEntity[FlytCoordinator], SensorEntity):
    """Account balance for an agreement (negative = owed to Flyt)."""

    _attr_has_entity_name = True
    _attr_translation_key = "balance"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_suggested_display_precision = 2

    def __init__(self, coordinator: FlytCoordinator, agreement_id: str) -> None:
        super().__init__(coordinator)
        self._agreement_id = agreement_id
        agr = coordinator.data.agreements[agreement_id]
        self._attr_unique_id = f"agreement_{agreement_id}_balance"
        self._attr_native_unit_of_measurement = agr.currency
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"agreement_{agreement_id}")},
            name=f"Flyt avtale {agr.agreement_number} ({agr.customer_name})",
            manufacturer="Flyt",
            model="Avtale",
        )

    @property
    def _agr(self) -> Agreement | None:
        return self.coordinator.data.agreements.get(self._agreement_id)

    @property
    def native_value(self) -> float | None:
        agr = self._agr
        return agr.balance if agr else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        agr = self._agr
        if agr is None:
            return None
        return {
            "agreement_number": agr.agreement_number,
            "customer": agr.customer_name,
            "customer_number": agr.customer_number,
        }
