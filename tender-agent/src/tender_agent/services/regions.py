"""Canonical UK region assignment from OCDS delivery-address data (Phase 4 chunk 8).

The standard region is almost always ALREADY present in the raw OCDS payload — we
just weren't extracting it. Each tender carries one or more delivery addresses
(`raw.tender.items[].deliveryAddresses[]`, plus awards/other items) that contain
either:

  - a ``region`` (e.g. ``"North West"`` — already a canonical name, or a NUTS code
    like ``"UKM82"``), or
  - a ``postalCode`` (e.g. ``"M1 3BN"``), or
  - just a ``countryName`` (e.g. ``"Vietnam"``, ``"United Kingdom"``).

Resolution is therefore mostly EXACT EXTRACTION, with a deterministic postcode
fallback and a country fallback. An LLM is only ever consulted for a free-text
location string that none of the deterministic paths resolve — and the
``region_lookup`` cache guarantees each distinct unresolved key is asked once.

Priority per tender (see ``resolve_region``):
  a. OCDS region (exact / NUTS code)  -> method ``ocds_region``
  b. UK postcode area                  -> method ``postcode``
  c. country name                      -> method ``country``
  d. AI fallback (rare, cached)        -> method ``ai``
  e. nothing usable                    -> ``Unspecified`` / method ``unspecified``

This module is source-agnostic: it works off whatever ``raw`` an adapter stored
plus the canonical ``buyer_region`` / ``buyer_country`` columns. OCDS sources use
``deliveryAddresses``; a non-OCDS source can be supported later by teaching
``_iter_delivery_addresses`` (or the caller) where that source keeps its
location, then reusing the same maps below.
"""
from __future__ import annotations

import asyncio
import re
from collections.abc import Iterator
from dataclasses import dataclass

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from tender_agent.models import RegionLookup, Tender

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Canonical region list — the single source of truth (exact strings, ordered).
# ---------------------------------------------------------------------------

CANONICAL_REGIONS: tuple[str, ...] = (
    "United Kingdom",
    "England",
    "North East",
    "North West",
    "Yorkshire and the Humber",
    "East Midlands",
    "West Midlands",
    "East of England",
    "London",
    "South East",
    "South West",
    "Scotland",
    "Wales",
    "Northern Ireland",
    "Isle of Man",
    "Channel Islands",
    "British Overseas Territories",
    "Europe",
    "Rest of the World",
    "Unspecified",
)

UNSPECIFIED = "Unspecified"

_CANONICAL_BY_LOWER: dict[str, str] = {r.lower(): r for r in CANONICAL_REGIONS}

# Recognised methods, in priority order. Used for log/count keys.
METHODS = ("ocds_region", "postcode", "country", "ai", "unspecified")

# ---------------------------------------------------------------------------
# NUTS first-level (ITL1) codes -> canonical region. The OCDS `region` field
# frequently holds these (e.g. "UKM82" -> Scotland). The leading three chars
# of any UK NUTS code identify the region.
# ---------------------------------------------------------------------------

NUTS1_TO_REGION: dict[str, str] = {
    "UKC": "North East",
    "UKD": "North West",
    "UKE": "Yorkshire and the Humber",
    "UKF": "East Midlands",
    "UKG": "West Midlands",
    "UKH": "East of England",
    "UKI": "London",
    "UKJ": "South East",
    "UKK": "South West",
    "UKL": "Wales",
    "UKM": "Scotland",
    "UKN": "Northern Ireland",
}

# ---------------------------------------------------------------------------
# UK postcode AREA (leading letters of the outward code) -> canonical region.
# Reasonably complete; border areas are assigned to their dominant region.
# ---------------------------------------------------------------------------

POSTCODE_AREA_TO_REGION: dict[str, str] = {
    # North East
    "NE": "North East", "SR": "North East", "DH": "North East",
    "DL": "North East", "TS": "North East",
    # North West
    "CA": "North West", "LA": "North West", "PR": "North West",
    "BB": "North West", "BL": "North West", "FY": "North West",
    "M": "North West", "OL": "North West", "SK": "North West",
    "WN": "North West", "WA": "North West", "L": "North West",
    "CH": "North West", "CW": "North West",
    # Yorkshire and the Humber
    "BD": "Yorkshire and the Humber", "HD": "Yorkshire and the Humber",
    "HG": "Yorkshire and the Humber", "HU": "Yorkshire and the Humber",
    "HX": "Yorkshire and the Humber", "LS": "Yorkshire and the Humber",
    "S": "Yorkshire and the Humber", "WF": "Yorkshire and the Humber",
    "YO": "Yorkshire and the Humber", "DN": "Yorkshire and the Humber",
    # East Midlands
    "DE": "East Midlands", "LE": "East Midlands", "LN": "East Midlands",
    "NG": "East Midlands", "NN": "East Midlands",
    # West Midlands
    "B": "West Midlands", "CV": "West Midlands", "DY": "West Midlands",
    "HR": "West Midlands", "ST": "West Midlands", "TF": "West Midlands",
    "WS": "West Midlands", "WV": "West Midlands", "WR": "West Midlands",
    "SY": "West Midlands",
    # East of England
    "AL": "East of England", "CB": "East of England", "CM": "East of England",
    "CO": "East of England", "IP": "East of England", "LU": "East of England",
    "PE": "East of England", "SG": "East of England", "SS": "East of England",
    "NR": "East of England", "WD": "East of England",
    # London
    "E": "London", "EC": "London", "N": "London", "NW": "London",
    "SE": "London", "SW": "London", "W": "London", "WC": "London",
    "BR": "London", "CR": "London", "EN": "London", "HA": "London",
    "IG": "London", "RM": "London", "SM": "London", "UB": "London",
    # South East
    "BN": "South East", "CT": "South East", "DA": "South East",
    "GU": "South East", "HP": "South East", "KT": "South East",
    "ME": "South East", "MK": "South East", "OX": "South East",
    "PO": "South East", "RG": "South East", "RH": "South East",
    "SL": "South East", "SO": "South East", "TN": "South East",
    "TW": "South East",
    # South West
    "BA": "South West", "BH": "South West", "BS": "South West",
    "DT": "South West", "EX": "South West", "GL": "South West",
    "PL": "South West", "SN": "South West", "SP": "South West",
    "TA": "South West", "TQ": "South West", "TR": "South West",
    # Wales
    "CF": "Wales", "LD": "Wales", "LL": "Wales", "NP": "Wales",
    "SA": "Wales",
    # Scotland
    "AB": "Scotland", "DD": "Scotland", "DG": "Scotland", "EH": "Scotland",
    "FK": "Scotland", "G": "Scotland", "HS": "Scotland", "IV": "Scotland",
    "KA": "Scotland", "KW": "Scotland", "KY": "Scotland", "ML": "Scotland",
    "PA": "Scotland", "PH": "Scotland", "TD": "Scotland", "ZE": "Scotland",
    # Northern Ireland / islands
    "BT": "Northern Ireland",
    "IM": "Isle of Man",
    "GY": "Channel Islands", "JE": "Channel Islands",
}

# ---------------------------------------------------------------------------
# Country-name handling.
# ---------------------------------------------------------------------------

_UK_ALIASES = {
    "uk", "u.k.", "gb", "gbr", "great britain", "britain",
    "united kingdom of great britain and northern ireland",
}
_CHANNEL_ISLANDS = {"guernsey", "jersey", "alderney", "sark", "channel islands"}
_ISLE_OF_MAN = {"isle of man"}

# Non-UK European countries -> "Europe". Lower-cased. Reasonably complete; any
# European country not listed still falls through to "Rest of the World", which
# is an acceptable coarse default for the rare long tail.
_EUROPEAN_COUNTRIES = {
    "albania", "andorra", "austria", "belarus", "belgium",
    "bosnia and herzegovina", "bosnia", "bulgaria", "croatia", "cyprus",
    "czech republic", "czechia", "denmark", "estonia", "faroe islands",
    "finland", "france", "germany", "gibraltar", "greece", "hungary",
    "iceland", "ireland", "republic of ireland", "italy", "kosovo", "latvia",
    "liechtenstein", "lithuania", "luxembourg", "malta", "moldova", "monaco",
    "montenegro", "netherlands", "the netherlands", "north macedonia",
    "macedonia", "norway", "poland", "portugal", "romania", "russia",
    "russian federation", "san marino", "serbia", "slovakia", "slovenia",
    "spain", "sweden", "switzerland", "ukraine", "vatican city",
    "holy see",
}

_BOT_SPELLINGS = {
    "british oversea territories": "British Overseas Territories",
    "british overseas territory": "British Overseas Territories",
}

_NUTS_RE = re.compile(r"^UK([CDEFGHIJKLMN])")
_POSTCODE_AREA_RE = re.compile(r"^\s*([A-Za-z]{1,2})")


def _fix_spelling(value: str) -> str:
    """Normalise the known mis-spelling(s) in the raw data."""
    fixed = _BOT_SPELLINGS.get(value.strip().lower())
    return fixed if fixed else value.strip()


def _match_region_string(value: str | None) -> str | None:
    """Map a raw region string to a canonical region by EXACT name (case-
    insensitive) or NUTS code. Returns None if it is neither."""
    if not value:
        return None
    fixed = _fix_spelling(value)
    low = fixed.lower()
    if low in _CANONICAL_BY_LOWER:
        return _CANONICAL_BY_LOWER[low]
    up = fixed.upper()
    if up == "UK":
        return "United Kingdom"
    m = _NUTS_RE.match(up)
    if m:
        return NUTS1_TO_REGION.get(up[:3])
    return None


def _postcode_area(postcode: str | None) -> str | None:
    """Return the postcode AREA (leading letters of the outward code), upper-cased."""
    if not postcode:
        return None
    m = _POSTCODE_AREA_RE.match(postcode)
    return m.group(1).upper() if m else None


def _postcode_to_region(postcode: str | None) -> str | None:
    area = _postcode_area(postcode)
    if not area:
        return None
    return POSTCODE_AREA_TO_REGION.get(area)


def _country_to_region(country: str | None) -> str | None:
    """Map a country name to a canonical region. Any non-empty country resolves
    (UK/home nation -> itself; European -> Europe; everything else -> Rest of
    the World)."""
    if not country or not country.strip():
        return None
    fixed = _fix_spelling(country)
    low = fixed.lower()
    # Home nations / "United Kingdom" / "British Overseas Territories" are
    # themselves canonical region names.
    canon = _match_region_string(fixed)
    if canon:
        return canon
    if low in _UK_ALIASES:
        return "United Kingdom"
    if low in _CHANNEL_ISLANDS:
        return "Channel Islands"
    if low in _ISLE_OF_MAN:
        return "Isle of Man"
    if low in _EUROPEAN_COUNTRIES:
        return "Europe"
    return "Rest of the World"


# ---------------------------------------------------------------------------
# Signal extraction from raw OCDS.
# ---------------------------------------------------------------------------


def _collect_addresses(obj: object, out: list[dict]) -> None:
    if not isinstance(obj, dict):
        return
    single = obj.get("deliveryAddress")
    if isinstance(single, dict):
        out.append(single)
    many = obj.get("deliveryAddresses")
    if isinstance(many, list):
        out.extend(a for a in many if isinstance(a, dict))


def _iter_delivery_addresses(raw: dict | None) -> Iterator[dict]:
    """Yield every delivery address dict found anywhere useful in an OCDS release:
    tender-level, tender items, award-level, and award items."""
    if not isinstance(raw, dict):
        return
    out: list[dict] = []
    tender = raw.get("tender")
    if isinstance(tender, dict):
        _collect_addresses(tender, out)
        for item in tender.get("items") or []:
            _collect_addresses(item, out)
    for award in raw.get("awards") or []:
        _collect_addresses(award, out)
        if isinstance(award, dict):
            for item in award.get("items") or []:
                _collect_addresses(item, out)
    _collect_addresses(raw, out)
    yield from out


@dataclass(slots=True)
class _Signals:
    regions: list[str]
    postcodes: list[str]
    countries: list[str]


def _extract_signals(
    raw: dict | None, buyer_region: str | None, buyer_country: str | None
) -> _Signals:
    regions: list[str] = []
    postcodes: list[str] = []
    countries: list[str] = []
    for addr in _iter_delivery_addresses(raw):
        if addr.get("region"):
            regions.append(str(addr["region"]))
        if addr.get("postalCode"):
            postcodes.append(str(addr["postalCode"]))
        country = addr.get("countryName") or addr.get("country")
        if country:
            countries.append(str(country))
    # Buyer columns come last — the delivery address is more precise, and
    # buyer_region is typically a messy city/locality.
    if buyer_region:
        regions.append(str(buyer_region))
    if buyer_country:
        countries.append(str(buyer_country))
    return _Signals(regions=regions, postcodes=postcodes, countries=countries)


# ---------------------------------------------------------------------------
# Resolution.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RegionResolution:
    """Result of resolving a tender's region.

    ``region`` is always a canonical region (``Unspecified`` when nothing
    resolved). ``method`` records how. When deterministic resolution fails but
    a free-text location string exists, ``ai_candidate`` holds that string so an
    async caller can run the (cached) AI fallback."""

    region: str
    method: str
    ai_candidate: str | None = None


class RegionLookupCache:
    """De-dupes resolution of distinct location keys across a run.

    Backed by the ``region_lookup`` table when a Session is supplied (so a key
    resolved on a previous run — including the rare AI calls — is never re-asked),
    with an in-memory layer on top. Pass ``db=None`` for a pure in-memory cache
    (used in unit tests). New rows are added to the session but NOT committed;
    the caller controls the transaction boundary."""

    def __init__(self, db: Session | None) -> None:
        self._db = db
        self._mem: dict[str, tuple[str, str]] = {}

    def get(self, key: str) -> tuple[str, str] | None:
        """Return (canonical_region, method) for ``key`` if known."""
        if key in self._mem:
            return self._mem[key]
        if self._db is not None:
            row = self._db.execute(
                select(RegionLookup).where(RegionLookup.raw_key == key)
            ).scalar_one_or_none()
            if row is not None:
                self._mem[key] = (row.canonical_region, row.method)
                return self._mem[key]
        return None

    def put(self, key: str, region: str, method: str) -> None:
        if key in self._mem:
            return
        self._mem[key] = (region, method)
        if self._db is not None:
            self._db.add(
                RegionLookup(raw_key=key, canonical_region=region, method=method)
            )
            self._db.flush()


def resolve_region(
    raw: dict | None,
    buyer_region: str | None,
    buyer_country: str | None,
    *,
    cache: RegionLookupCache | None = None,
) -> RegionResolution:
    """Deterministic region resolution (no AI). Implements priority a->c and
    sets ``ai_candidate`` for the rare d/e cases so a caller can optionally run
    the AI fallback. Postcode and country resolutions are cached when a cache is
    supplied."""
    signals = _extract_signals(raw, buyer_region, buyer_country)

    # a. OCDS region — exact name or NUTS code. No cache needed: pure lookup.
    unresolved_region: str | None = None
    for value in signals.regions:
        canon = _match_region_string(value)
        if canon:
            return RegionResolution(canon, "ocds_region")
        if unresolved_region is None and value.strip():
            unresolved_region = value.strip()

    # b. UK postcode area.
    for postcode in signals.postcodes:
        area = _postcode_area(postcode)
        if not area:
            continue
        key = f"postcode:{area}"
        if cache and (hit := cache.get(key)):
            return RegionResolution(hit[0], hit[1])
        region = POSTCODE_AREA_TO_REGION.get(area)
        if region:
            if cache:
                cache.put(key, region, "postcode")
            return RegionResolution(region, "postcode")

    # c. Country name.
    for country in signals.countries:
        key = f"country:{country.strip().lower()}"
        if cache and (hit := cache.get(key)):
            return RegionResolution(hit[0], hit[1])
        region = _country_to_region(country)
        if region:
            if cache:
                cache.put(key, region, "country")
            return RegionResolution(region, "country")

    # d/e. Nothing deterministic resolved. If there was a free-text location
    # string, hand it to the AI fallback (if a caller wants to); else Unspecified.
    return RegionResolution(UNSPECIFIED, "unspecified", ai_candidate=unresolved_region)


def resolve_for_tender(
    raw: dict | None,
    buyer_region: str | None,
    buyer_country: str | None,
    *,
    cache: RegionLookupCache | None = None,
) -> str:
    """Convenience deterministic resolver returning just the canonical region.

    Used by the ingestion hook — deterministic only, never calls an external
    API in the polling hot path. The rare unresolved tenders become
    ``Unspecified`` and can be upgraded later by the backfill (which is allowed
    to use the AI fallback)."""
    return resolve_region(raw, buyer_region, buyer_country, cache=cache).region


# ---------------------------------------------------------------------------
# AI fallback (rare, cached). Reuses the brief LLM client.
# ---------------------------------------------------------------------------

_AI_SYSTEM = (
    "You assign a UK public-procurement delivery location to exactly one region "
    "from a fixed list. Reply with ONLY the exact region name from the list, and "
    "nothing else. If the location is unclear or not a real place, reply "
    "'Unspecified'."
)


def _build_ai_user(location: str) -> str:
    options = "\n".join(f"- {r}" for r in CANONICAL_REGIONS)
    return (
        f"Location: {location!r}\n\n"
        f"Choose exactly one region from this list:\n{options}\n\n"
        "Region:"
    )


async def _resolve_via_ai(location: str, llm: object) -> str:
    """Ask the LLM to map a free-text location to one canonical region. Returns
    a canonical region, or 'Unspecified' if the answer is junk."""
    response = await llm.complete(  # type: ignore[attr-defined]
        system=_AI_SYSTEM, user=_build_ai_user(location), max_tokens=24
    )
    text = (response.text or "").strip()
    canon = _match_region_string(text)
    if canon:
        return canon
    low = text.lower()
    if low in _CANONICAL_BY_LOWER:
        return _CANONICAL_BY_LOWER[low]
    return UNSPECIFIED


async def resolve_region_ai(
    raw: dict | None,
    buyer_region: str | None,
    buyer_country: str | None,
    *,
    cache: RegionLookupCache | None = None,
    llm: object | None = None,
) -> RegionResolution:
    """Full resolution including the AI fallback. Deterministic first; only an
    unresolved free-text location with an ``llm`` supplied triggers an AI call,
    and the cache guarantees one call per distinct key."""
    det = resolve_region(raw, buyer_region, buyer_country, cache=cache)
    if det.method != "unspecified" or not det.ai_candidate:
        return det
    key = f"ai:{det.ai_candidate.strip().lower()}"
    if cache and (hit := cache.get(key)):
        return RegionResolution(hit[0], hit[1])
    if llm is None:
        return det
    region = await _resolve_via_ai(det.ai_candidate, llm)
    if cache:
        cache.put(key, region, "ai")
    return RegionResolution(region, "ai")


def _resolve_sync(
    raw: dict | None,
    buyer_region: str | None,
    buyer_country: str | None,
    *,
    cache: RegionLookupCache,
    llm: object | None,
) -> RegionResolution:
    """Synchronous resolution used by the backfill: deterministic, with the AI
    fallback run via ``asyncio.run`` only when needed (and not already cached)."""
    det = resolve_region(raw, buyer_region, buyer_country, cache=cache)
    if det.method != "unspecified" or not det.ai_candidate:
        return det
    key = f"ai:{det.ai_candidate.strip().lower()}"
    if hit := cache.get(key):
        return RegionResolution(hit[0], hit[1])
    if llm is None:
        return det
    region = asyncio.run(_resolve_via_ai(det.ai_candidate, llm))
    cache.put(key, region, "ai")
    return RegionResolution(region, "ai")


# ---------------------------------------------------------------------------
# Bulk backfill.
# ---------------------------------------------------------------------------


def normalise_all_tenders(
    db: Session,
    *,
    llm: object | None = None,
    batch_size: int = 500,
) -> dict[str, int]:
    """Set ``region`` for every tender where it is currently NULL, using the
    cache so each distinct key is resolved once. Returns counts keyed by method
    (``ocds_region`` / ``postcode`` / ``country`` / ``ai`` / ``unspecified``).

    Re-runnable: only NULL-region rows are touched, so a second run is a no-op
    unless new tenders arrived. Commits per batch."""
    cache = RegionLookupCache(db)
    counts: dict[str, int] = dict.fromkeys(METHODS, 0)

    while True:
        rows = list(
            db.execute(
                select(Tender)
                .where(Tender.region.is_(None))
                .order_by(Tender.id.asc())
                .limit(batch_size)
            ).scalars().all()
        )
        if not rows:
            break
        for tender in rows:
            res = _resolve_sync(
                tender.raw,
                tender.buyer_region,
                tender.buyer_country,
                cache=cache,
                llm=llm,
            )
            tender.region = res.region
            counts[res.method] = counts.get(res.method, 0) + 1
        db.commit()
        logger.info(
            "regions.normalise_batch",
            processed=sum(counts.values()),
            **counts,
        )

    logger.info("regions.normalise_complete", **counts)
    return counts
