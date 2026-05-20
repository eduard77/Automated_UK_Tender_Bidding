"""Portal discovery — extract external URLs from a tender, normalise them,
filter out blocklisted noise, and persist Portal + PortalUrlSighting rows.

Run inline at ingestion time and also from `scripts/backfill_portal_discovery.py`.

Design notes:

* Extraction is purely mechanical. Classification (is_procurement_portal,
  login_type, priority) is delegated to `portal_classifier.py` and runs
  asynchronously.
* `tender_count` on a portal counts *distinct* tenders, not raw sightings.
  Updated only when a (portal_id, tender_id) pair is new.
* The blocklist is database-driven; the dashboard owns it. We treat `gov.uk`
  as a special case — the apex is blocked but procurement subdomains
  (e.g. `nepo.gov.uk`, `crowncommercial.gov.uk`) are kept.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlparse, urlunparse

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from tender_agent.models import (
    Portal,
    PortalBlocklistDomain,
    PortalUrlSighting,
    Tender,
)
from tender_agent.services.platform_matching import find_platform_for_domain

logger = structlog.get_logger(__name__)


# --- URL / email patterns -----------------------------------------------

# Reasonably permissive URL pattern. We strip trailing punctuation
# (`.`, `,`, `;`, closing brackets) after extraction; this is easier than
# encoding their absence into the regex and copes with URLs glued onto the
# end of a sentence.
URL_REGEX = re.compile(
    r"\bhttps?://[^\s<>\"'\)\]\}]+",
    re.IGNORECASE,
)

EMAIL_REGEX = re.compile(
    r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b",
)

_TRAILING_JUNK = ".,;:!?)]}>'\""

# Query-string keys we keep when normalising — these typically carry the
# tender identifier on a portal (e.g. ?tender=12345 or ?id=abc).
_KEEP_QUERY_KEYS = ("tender", "id", "ref")


# --- Dataclasses --------------------------------------------------------


@dataclass(frozen=True)
class ExtractedUrl:
    url: str
    normalised_url: str
    domain: str
    extracted_from: str  # one of ExtractedFrom values
    sighting_type: str  # one of SightingType values


@dataclass(frozen=True)
class ProcessResult:
    new_portals: int
    new_sightings: int
    classifications_queued: int
    portal_ids_queued: list[int]


# --- Public helpers -----------------------------------------------------


def normalise_url(raw: str) -> tuple[str, str] | None:
    """Lowercase scheme + host, strip fragments, keep a small allowlist of
    query keys. Returns (normalised_url, bare_domain) or None if unparseable
    or scheme is not http(s).
    """
    cleaned = raw.strip().rstrip(_TRAILING_JUNK)
    if not cleaned:
        return None
    try:
        parsed = urlparse(cleaned)
    except ValueError:
        return None
    if parsed.scheme.lower() not in ("http", "https"):
        return None
    if not parsed.netloc:
        return None

    host = parsed.hostname or ""
    host = host.lower().lstrip(".")
    # Strip www. so we cluster cleanly.
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return None

    # Rebuild query keeping only useful keys.
    kept_query = ""
    if parsed.query:
        keep_pairs: list[str] = []
        for pair in parsed.query.split("&"):
            key = pair.split("=", 1)[0].lower()
            if any(k in key for k in _KEEP_QUERY_KEYS):
                keep_pairs.append(pair)
        kept_query = "&".join(keep_pairs)

    port = f":{parsed.port}" if parsed.port and parsed.port not in (80, 443) else ""
    netloc = host + port
    normalised = urlunparse(
        (parsed.scheme.lower(), netloc, parsed.path or "", "", kept_query, "")
    )
    return normalised, host


def is_gov_uk_apex(domain: str) -> bool:
    """`gov.uk` itself is informational; subdomains may be procurement."""
    parts = domain.split(".")
    return len(parts) == 2 and parts[-2:] == ["gov", "uk"]


def _load_blocklist(db: Session) -> set[str]:
    rows = db.execute(select(PortalBlocklistDomain.domain)).all()
    return {r[0].lower() for r in rows}


def _is_blocked(domain: str, blocklist: set[str]) -> bool:
    """True if the domain is blocklisted. The blocklist matches exactly OR by
    suffix for every entry except gov.uk: that one only blocks the apex
    (so we keep subdomains like nepo.gov.uk).
    """
    domain = domain.lower()
    for blocked in blocklist:
        if blocked == "gov.uk":
            if is_gov_uk_apex(domain):
                return True
            continue
        if domain == blocked or domain.endswith("." + blocked):
            return True
    return False


# --- Extraction ---------------------------------------------------------


def _iter_text_urls(
    text: str | None,
    extracted_from: str,
) -> Iterable[tuple[str, str]]:
    """Yield (raw_url, sighting_type) for every URL or email in `text`."""
    if not text:
        return
    for m in URL_REGEX.finditer(text):
        yield m.group(0), "tender_link"
    for m in EMAIL_REGEX.finditer(text):
        yield f"mailto:{m.group(0)}", "contact_email"


def _iter_raw_party_urls(raw: dict | None) -> Iterable[tuple[str, str, str]]:
    """Pull contactPoint.url / contactPoint.email from raw OCDS-ish payloads.
    Yields (raw_value, extracted_from, sighting_type)."""
    if not isinstance(raw, dict):
        return
    # OCDS shape: {"releases": [{"parties": [{"contactPoint": {...}}, ...]}]}
    candidates: list[dict] = []
    releases = raw.get("releases")
    if isinstance(releases, list):
        for release in releases:
            if not isinstance(release, dict):
                continue
            parties = release.get("parties")
            if isinstance(parties, list):
                candidates.extend(p for p in parties if isinstance(p, dict))
    # Some adapters store a flat dict already.
    parties = raw.get("parties")
    if isinstance(parties, list):
        candidates.extend(p for p in parties if isinstance(p, dict))

    seen: set[str] = set()
    for party in candidates:
        contact = party.get("contactPoint") or party.get("contact_point")
        if not isinstance(contact, dict):
            continue
        url = contact.get("url")
        if isinstance(url, str) and url and url not in seen:
            seen.add(url)
            yield url, "parties", "tender_link"
        email = contact.get("email")
        if isinstance(email, str) and email and email not in seen:
            seen.add(email)
            yield f"mailto:{email}", "parties", "contact_email"


def extract_urls_from_tender(
    tender: Tender, db: Session
) -> list[ExtractedUrl]:
    """Pull every candidate URL + email out of the tender's fields, normalise,
    deduplicate, and apply the database-driven blocklist.

    Returns an empty list if there's nothing useful to capture.
    """
    blocklist = _load_blocklist(db)

    # Source 1: description (URLs + emails).
    raw_candidates: list[tuple[str, str, str]] = []
    for raw_url, kind in _iter_text_urls(tender.description, "description"):
        raw_candidates.append((raw_url, "description", kind))

    # Source 2: additional_information in raw payload (some adapters surface it).
    raw_payload = tender.raw if isinstance(tender.raw, dict) else None
    addl = _find_additional_information(raw_payload)
    for raw_url, kind in _iter_text_urls(addl, "additional_information"):
        raw_candidates.append((raw_url, "additional_information", kind))

    # Source 3: documents — JSON list of {title, url, format}.
    for doc in tender.documents or []:
        if not isinstance(doc, dict):
            continue
        url = doc.get("url")
        if isinstance(url, str) and url:
            raw_candidates.append((url, "documents", "document_link"))

    # Source 4: parties[].contactPoint in raw.
    raw_candidates.extend(_iter_raw_party_urls(raw_payload))

    # Source 5: source_url itself — useful only if it's external. Skipped
    # because it's almost always our source domain (which is blocklisted).

    out: list[ExtractedUrl] = []
    seen_norm: set[tuple[str, str, str]] = set()

    for raw, extracted_from, sighting_type in raw_candidates:
        if raw.startswith("mailto:"):
            email = raw[len("mailto:") :].strip().rstrip(_TRAILING_JUNK).lower()
            if not email or "@" not in email:
                continue
            domain = email.split("@", 1)[1]
            if _is_blocked(domain, blocklist):
                continue
            key = (raw, extracted_from, sighting_type)
            if key in seen_norm:
                continue
            seen_norm.add(key)
            out.append(
                ExtractedUrl(
                    url=raw,
                    normalised_url=f"mailto:{email}",
                    domain=domain,
                    extracted_from=extracted_from,
                    sighting_type=sighting_type,
                )
            )
            continue

        normalised = normalise_url(raw)
        if normalised is None:
            continue
        norm_url, domain = normalised
        if _is_blocked(domain, blocklist):
            continue
        key = (norm_url, extracted_from, sighting_type)
        if key in seen_norm:
            continue
        seen_norm.add(key)
        out.append(
            ExtractedUrl(
                url=raw.rstrip(_TRAILING_JUNK),
                normalised_url=norm_url,
                domain=domain,
                extracted_from=extracted_from,
                sighting_type=sighting_type,
            )
        )

    return out


def _find_additional_information(raw: dict | None) -> str | None:
    """Best-effort lookup of additionalInformation in the raw payload — every
    adapter stores it slightly differently."""
    if not isinstance(raw, dict):
        return None
    # Direct field used by some normalisers.
    direct = raw.get("additional_information") or raw.get("additionalInformation")
    if isinstance(direct, str) and direct:
        return direct
    # OCDS shape under releases[].tender.additionalProcurementCategories etc.
    releases = raw.get("releases")
    if isinstance(releases, list):
        chunks: list[str] = []
        for release in releases:
            if not isinstance(release, dict):
                continue
            tender_blob = release.get("tender")
            if isinstance(tender_blob, dict):
                info = tender_blob.get("additionalInformation") or tender_blob.get(
                    "description"
                )
                if isinstance(info, str) and info:
                    chunks.append(info)
        return "\n".join(chunks) or None
    return None


# --- Persistence --------------------------------------------------------


LINK_SIGHTING_TYPES = ("tender_link", "document_link")


def _get_or_create_portal(
    db: Session, domain: str
) -> tuple[Portal, bool]:
    portal = db.execute(
        select(Portal).where(Portal.domain == domain)
    ).scalar_one_or_none()
    if portal is not None:
        # Backfill platform link if a matching platform was seeded after the
        # portal was first discovered.
        if portal.platform_id is None:
            platform = find_platform_for_domain(db, domain)
            if platform is not None:
                portal.platform_id = platform.id
        return portal, False
    platform = find_platform_for_domain(db, domain)
    portal = Portal(
        domain=domain,
        display_name=domain,
        platform_id=platform.id if platform is not None else None,
        # url_patterns / classification_data left at SQL defaults.
    )
    db.add(portal)
    db.flush()
    return portal, True


def _portal_has_sighting_type(
    db: Session, portal_id: int, sighting_types: tuple[str, ...]
) -> bool:
    return (
        db.execute(
            select(PortalUrlSighting.id)
            .where(PortalUrlSighting.portal_id == portal_id)
            .where(PortalUrlSighting.sighting_type.in_(sighting_types))
            .limit(1)
        ).scalar_one_or_none()
        is not None
    )


def process_tender_for_portals(
    tender: Tender, db: Session
) -> ProcessResult:
    """Run extraction + persistence for one tender. Idempotent: re-running on
    the same tender won't double-count.
    """
    extracted = extract_urls_from_tender(tender, db)
    if not extracted:
        return ProcessResult(0, 0, 0, [])

    now = datetime.now(UTC)
    new_portals = 0
    new_sightings = 0
    classifications_queued = 0
    queued_ids: list[int] = []

    # Group by domain so we touch each portal record once.
    by_domain: dict[str, list[ExtractedUrl]] = {}
    for e in extracted:
        by_domain.setdefault(e.domain, []).append(e)

    for domain, urls in by_domain.items():
        portal, created = _get_or_create_portal(db, domain)
        if created:
            new_portals += 1
            classifications_queued += 1
            queued_ids.append(portal.id)

        # tender_count counts a tender once it has at least one *link-type*
        # sighting (tender_link / document_link) on this portal. Pure
        # contact_email / reference_text sightings never count — that was the
        # v1 bug that inflated email domains like nhs.net.
        had_link_before = (
            db.execute(
                select(PortalUrlSighting.id)
                .where(PortalUrlSighting.portal_id == portal.id)
                .where(PortalUrlSighting.tender_id == tender.id)
                .where(PortalUrlSighting.sighting_type.in_(LINK_SIGHTING_TYPES))
                .limit(1)
            ).scalar_one_or_none()
            is not None
        )
        inserted_link = False

        for u in urls:
            # Idempotency at the sighting level: skip if (portal, tender, url,
            # extracted_from, sighting_type) already exists.
            existing = db.execute(
                select(PortalUrlSighting.id)
                .where(PortalUrlSighting.portal_id == portal.id)
                .where(PortalUrlSighting.tender_id == tender.id)
                .where(PortalUrlSighting.url == u.normalised_url)
                .where(PortalUrlSighting.extracted_from == u.extracted_from)
                .where(PortalUrlSighting.sighting_type == u.sighting_type)
                .limit(1)
            ).scalar_one_or_none()
            if existing is not None:
                continue
            db.add(
                PortalUrlSighting(
                    portal_id=portal.id,
                    tender_id=tender.id,
                    url=u.normalised_url,
                    extracted_from=u.extracted_from,
                    sighting_type=u.sighting_type,
                    extracted_at=now,
                )
            )
            new_sightings += 1
            if u.sighting_type in LINK_SIGHTING_TYPES:
                inserted_link = True

        # Only bump when this (portal, tender) pair transitions from "no link"
        # to "has link".
        if not had_link_before and inserted_link:
            portal.tender_count = (portal.tender_count or 0) + 1

        db.flush()  # ensure new sightings are visible to the queries below
        # Recompute is_email_domain: true iff every sighting is contact_email.
        has_link = had_link_before or inserted_link or _portal_has_sighting_type(
            db, portal.id, LINK_SIGHTING_TYPES
        )
        if has_link:
            portal.is_email_domain = False
        else:
            portal.is_email_domain = _portal_has_sighting_type(
                db, portal.id, ("contact_email",)
            )
        portal.last_seen_at = now
        portal.updated_at = now

    db.flush()

    return ProcessResult(
        new_portals=new_portals,
        new_sightings=new_sightings,
        classifications_queued=classifications_queued,
        portal_ids_queued=queued_ids,
    )
