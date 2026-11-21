"""Data update coordinator for Flyt."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import FlytAuthError, FlytClient, FlytError
from .const import DOMAIN, EVENT_NEW_PASSING, LOOKBACK_DAYS, TIMEZONE
from .locations import KIND_TOLL, fetch_index, locate

LOCATIONS_STORE_KEY = f"{DOMAIN}_locations"
LOCATIONS_STORE_VERSION = 1
LOCATIONS_MAX_AGE = timedelta(days=7)
LOCATIONS_RETRY = timedelta(hours=6)

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
    kind: str = KIND_TOLL  # "toll" or "ferry"
    latitude: float | None = None
    longitude: float | None = None
    location_name: str | None = None  # station/quay name in NVDB

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
            "kind": self.kind,
            "latitude": self.latitude,
            "longitude": self.longitude,
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

    @property
    def last_located_passing(self) -> Passing | None:
        return next((p for p in self.passings if p.latitude is not None), None)

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


def _parse_passing(
    raw: dict[str, Any], index: dict[str, Any] | None
) -> Passing | None:
    ts = raw.get("transactedOn") or raw.get("exitTollStationTransittedOn")
    if not ts:
        return None
    parsed = dt_util.parse_datetime(ts.replace(" ", "T"))
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        # The API returns Norwegian local time without an offset
        parsed = parsed.replace(tzinfo=dt_util.get_time_zone(TIMEZONE))
    kind, loc = locate(index, raw)
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
        kind=kind,
        latitude=loc.latitude if loc else None,
        longitude=loc.longitude if loc else None,
        location_name=loc.name if loc else None,
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
        self._store: Store[dict[str, Any]] = Store(
            hass, LOCATIONS_STORE_VERSION, LOCATIONS_STORE_KEY
        )
        self._index: dict[str, Any] | None = None
        self._index_fetched: datetime | None = None
        self._index_attempt: datetime | None = None

    async def _ensure_locations(self) -> None:
        """Load the NVDB index from disk, refresh it weekly. Never fatal."""
        now = dt_util.utcnow()
        if self._index is None and self._index_attempt is None:
            stored = await self._store.async_load()
            if stored and stored.get("index"):
                self._index = stored["index"]
                self._index_fetched = dt_util.parse_datetime(stored.get("fetched", ""))
        fresh = self._index_fetched and now - self._index_fetched < LOCATIONS_MAX_AGE
        retry_wait = self._index_attempt and now - self._index_attempt < LOCATIONS_RETRY
        if fresh or retry_wait:
            return
        self._index_attempt = now
        try:
            index = await fetch_index(async_get_clientsession(self.hass))
        except Exception as err:  # noqa: BLE001 - positions are optional
            _LOGGER.warning("Could not fetch toll station positions from NVDB: %s", err)
            return
        self._index, self._index_fetched = index, now
        await self._store.async_save({"fetched": now.isoformat(), "index": index})

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

        await self._ensure_locations()
        now = dt_util.now(dt_util.get_time_zone(TIMEZONE))
        since = min(
            now - timedelta(days=LOOKBACK_DAYS),
            now.replace(day=1, hour=0, minute=0, second=0, microsecond=0),
        )
        raw_passings = await self.client.get_passings(
            list(obes), date_from=dt_util.as_utc(since)
        )
        passings = [
            p for p in (_parse_passing(r, self._index) for r in raw_passings) if p
        ]
        unlocated = {p.station for p in passings if p.latitude is None}
        if unlocated:
            _LOGGER.debug("No position found for: %s", ", ".join(sorted(map(str, unlocated))))
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
