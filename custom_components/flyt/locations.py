"""Positions of toll stations and ferry quays from NVDB (Statens vegvesen).

NVDB is the open Norwegian road database. Toll stations (object type 45)
carry the same operator ID and station ID as the AutoPASS passings from Flyt,
so they match exactly. Ferry quays (type 64) have no AutoPASS ID, so ferry
passings are matched on the name of the departure quay.

No Home Assistant imports, so this can be tested standalone.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

NVDB_URL = "https://nvdbapiles.atlas.vegvesen.no/vegobjekter/api/v4/vegobjekter"
NVDB_HEADERS = {
    # NVDB asks clients to identify themselves; keep it generic
    "X-Client": "home-assistant-flyt",
    "Accept": "application/json",
}
TYPE_TOLL_STATION = 45
TYPE_FERRY_QUAY = 64

# Bump when the index format changes, so cached copies are refetched
INDEX_VERSION = 3

KIND_TOLL = "toll"
KIND_FERRY = "ferry"

_POINT = re.compile(r"POINT(?: Z)?\s*\(\s*([-\d.]+)\s+([-\d.]+)")


@dataclass
class Location:
    latitude: float
    longitude: float
    name: str


def _props(obj: dict[str, Any]) -> dict[str, Any]:
    return {e["navn"]: e.get("verdi") for e in obj.get("egenskaper", [])}


def _point(obj: dict[str, Any]) -> tuple[float, float] | None:
    # With srid=4326 NVDB returns "POINT Z (lat lon z)"
    match = _POINT.search((obj.get("geometri") or {}).get("wkt", ""))
    if not match:
        return None
    return float(match.group(1)), float(match.group(2))


def _tokens(name: str) -> set[str]:
    return set(re.findall(r"[a-zæøå]+|\d+", name.lower()))


def _norm(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().lower())


async def _fetch_type(
    session: aiohttp.ClientSession, type_id: int
) -> list[dict[str, Any]]:
    url: str | None = (
        f"{NVDB_URL}/{type_id}?inkluder=egenskaper,geometri&srid=4326&antall=1000"
    )
    objects: list[dict[str, Any]] = []
    for _ in range(50):  # safety limit on paging
        if not url:
            break
        async with session.get(
            url, headers=NVDB_HEADERS, timeout=aiohttp.ClientTimeout(total=60)
        ) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)
        objects.extend(data.get("objekter", []))
        meta = data.get("metadata", {})
        nxt = meta.get("neste")
        url = nxt.get("href") if nxt and meta.get("returnert") else None
    return objects


async def fetch_index(session: aiohttp.ClientSession) -> dict[str, Any]:
    """Download toll stations and ferry quays as a JSON-serialisable index."""
    # Several NVDB objects can share one ID pair (ramps, both sides of a
    # road), so every candidate is kept and the best name wins at lookup.
    toll: dict[str, list[list[Any]]] = {}
    for obj in await _fetch_type(session, TYPE_TOLL_STATION):
        props = _props(obj)
        point = _point(obj)
        operator, station = props.get("Operatør_Id"), props.get("Bomstasjon_Id")
        if point is None or operator is None or station is None:
            continue
        if "nedlagt" in str(props.get("Status", "")).lower():
            continue
        key = f"{int(operator)}:{int(station)}"
        toll.setdefault(key, []).append(
            [point[0], point[1], props.get("Navn bomstasjon") or ""]
        )

    quays: dict[str, list[Any]] = {}
    for obj in await _fetch_type(session, TYPE_FERRY_QUAY):
        props = _props(obj)
        point = _point(obj)
        name = props.get("Navn")
        if point is None or not name:
            continue
        quays[_norm(name)] = [point[0], point[1], name]

    _LOGGER.debug(
        "NVDB index: %d toll stations, %d ferry quays", len(toll), len(quays)
    )
    return {"version": INDEX_VERSION, "toll": toll, "quays": quays}


def _code(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _looks_like_ferry(raw: dict[str, Any]) -> bool:
    name = (raw.get("exitTollStationName") or "").lower()
    desc = (raw.get("description") or "").upper()
    return "ferje" in name or " kai" in name or desc.startswith("VEHICLE")


def _match_quay(index: dict[str, Any], quay_name: str) -> Location | None:
    quays: dict[str, list[Any]] = index.get("quays", {})
    norm = _norm(quay_name)
    if norm in quays:
        lat, lon, name = quays[norm]
        return Location(lat, lon, name)
    # "Brekstad kai" (Flyt) vs "Brekstad ferjekai" (NVDB): match on the
    # place name, but only when exactly one quay starts with it
    first = norm.split(" ")[0]
    if not first:
        return None
    candidates = [v for k, v in quays.items() if k.split(" ")[0] == first]
    if len(candidates) == 1:
        lat, lon, name = candidates[0]
        return Location(lat, lon, name)
    return None


def locate(
    index: dict[str, Any] | None, raw: dict[str, Any]
) -> tuple[str, Location | None]:
    """Return (kind, location) for a raw Flyt passing."""
    actor = _code(raw.get("exitTollStationActorId"))
    station = _code(raw.get("exitTollStationCode"))
    if index and actor is not None and station is not None:
        candidates = index.get("toll", {}).get(f"{actor}:{station}")
        if candidates:
            wanted = _tokens(raw.get("exitTollStationName") or "")
            lat, lon, name = max(
                candidates, key=lambda c: len(wanted & _tokens(c[2]))
            )
            return KIND_TOLL, Location(lat, lon, name)

    if _looks_like_ferry(raw):
        if not index:
            return KIND_FERRY, None
        # "Valset ferjekai-Brekstad kai": departure quay is before the dash
        departure = (raw.get("exitTollStationName") or "").split("-")[0]
        return KIND_FERRY, _match_quay(index, departure)

    return KIND_TOLL, None
