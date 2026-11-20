"""Data update coordinator for Flyt."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import FlytAuthError, FlytClient, FlytError
from .const import DOMAIN, EVENT_NEW_PASSING, LOOKBACK_DAYS, TIMEZONE

_LOGGER = logging.getLogger(__name__)


@dataclass
class Passing:
    """A single toll passing."""

    id: str
    obe_number: str
    time: datetime
    station: str | None
    operator: str | None
    price: float
    currency: str
    license_plate: str | None
    invoiced: bool
    description: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "obe_number": self.obe_number,
            "time": self.time.isoformat(),
            "station": self.station,
            "operator": self.operator,
            "price": self.price,
            "currency": self.currency,
            "license_plate": self.license_plate,
            "invoiced": self.invoiced,
        }


@dataclass
class Obe:
    """A toll tag (bombrikke) with its vehicle and recent passings."""

    obe_number: str
    status: str
    battery: str | None
    registration_number: str | None
    vehicle_name: str | None
    customer_name: str
    customer_number: str
    agreement_number: str | None
    passings: list[Passing] = field(default_factory=list)  # newest first

    @property
    def last_passing(self) -> Passing | None:
        return self.passings[0] if self.passings else None

    def month_passings(self, now: datetime) -> list[Passing]:
        return [
            p
            for p in self.passings
            if p.time.year == now.year and p.time.month == now.month
        ]


@dataclass
class Agreement:
    """An agreement (avtale) with its account balance."""

    id: str
    agreement_number: str
    customer_name: str
    customer_number: str
    status: str
    balance: float | None
    currency: str


@dataclass
class FlytData:
    obes: dict[str, Obe]
    agreements: dict[str, Agreement]


def _parse_passing(raw: dict[str, Any]) -> Passing | None:
    ts = raw.get("transactedOn") or raw.get("exitTollStationTransittedOn")
    if not ts:
        return None
    parsed = dt_util.parse_datetime(ts.replace(" ", "T"))
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        # The API returns Norwegian local time without an offset
        parsed = parsed.replace(tzinfo=dt_util.get_time_zone(TIMEZONE))
    price = float(raw.get("totalPrice") or 0)
    if raw.get("operationType") == "Debit":
        price = -price
    return Passing(
        id=raw["id"],
        obe_number=raw.get("obeNumber", ""),
        time=parsed,
        station=raw.get("exitTollStationName") or raw.get("entryTollStationName"),
        operator=raw.get("sender"),
        price=price,
        currency=raw.get("currency") or "NOK",
        license_plate=raw.get("licensePlateDetected"),
        invoiced=bool(raw.get("invoiced")),
        description=raw.get("description"),
    )


class FlytCoordinator(DataUpdateCoordinator[FlytData]):
    """Polls Flyt for tags, agreements and passings."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: FlytClient,
        interval: timedelta,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=interval,
        )
        self.client = client
        self._seen_ids: set[str] | None = None

    async def _async_update_data(self) -> FlytData:
        try:
            return await self._fetch()
        except FlytAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except FlytError as err:
            raise UpdateFailed(str(err)) from err
        except Exception as err:  # network errors etc.
            raise UpdateFailed(f"Error talking to Flyt: {err}") from err

    async def _fetch(self) -> FlytData:
        obes: dict[str, Obe] = {}
        agreements: dict[str, Agreement] = {}

        for customer in await self.client.get_customers():
            cname = customer.get("name") or customer.get("organizationName") or ""
            cnum = str(customer.get("customerNumber") or "")
            for agr in await self.client.get_agreements(customer["id"]):
                agreements[agr["id"]] = Agreement(
                    id=agr["id"],
                    agreement_number=str(agr.get("agreementNumber")),
                    customer_name=cname,
                    customer_number=cnum,
                    status=agr.get("status") or "",
                    balance=agr.get("accountBalance"),
                    currency=agr.get("accountCurrency") or "NOK",
                )
                for raw in agr.get("obes") or []:
                    vehicle = raw.get("vehicle") or {}
                    name = " ".join(
                        x for x in (vehicle.get("brand"), vehicle.get("model")) if x
                    )
                    obes[raw["obeNumber"]] = Obe(
                        obe_number=raw["obeNumber"],
                        status=raw.get("obeStatus") or "",
                        battery=raw.get("obeBatteryStatus"),
                        registration_number=vehicle.get("registrationNumber"),
                        vehicle_name=name.title() or None,
                        customer_name=cname,
                        customer_number=cnum,
                        agreement_number=str(agr.get("agreementNumber")),
                    )

        now = dt_util.now(dt_util.get_time_zone(TIMEZONE))
        since = min(
            now - timedelta(days=LOOKBACK_DAYS),
            now.replace(day=1, hour=0, minute=0, second=0, microsecond=0),
        )
        raw_passings = await self.client.get_passings(
            list(obes), date_from=dt_util.as_utc(since)
        )
        passings = [p for p in map(_parse_passing, raw_passings) if p]
        passings.sort(key=lambda p: p.time, reverse=True)
        for p in passings:
            if p.obe_number in obes:
                obes[p.obe_number].passings.append(p)

        self._fire_new_passing_events(passings, obes)
        return FlytData(obes=obes, agreements=agreements)

    def _fire_new_passing_events(
        self, passings: list[Passing], obes: dict[str, Obe]
    ) -> None:
        ids = {p.id for p in passings}
        if self._seen_ids is None:
            # First refresh after start: don't replay history as events
            self._seen_ids = ids
            return
        for p in reversed(passings):  # oldest first
            if p.id in self._seen_ids:
                continue
            obe = obes.get(p.obe_number)
            self.hass.bus.async_fire(
                EVENT_NEW_PASSING,
                {
                    **p.as_dict(),
                    "registration_number": obe.registration_number if obe else None,
                    "vehicle": obe.vehicle_name if obe else None,
                },
            )
        self._seen_ids = ids
