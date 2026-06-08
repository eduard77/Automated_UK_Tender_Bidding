"""Classify how a CF/FTS notice routes a supplier onward to a tender portal.

This is an **operator diagnostic**, not a product feature. It is pure string
classification — no database, no network, no side effects — so it can be unit
tested on plain strings and re-run cheaply. ``scripts/survey_cf_onward_routes.py``
feeds it text assembled from data already stored on each tender.

A Contracts Finder / Find a Tender notice usually tells suppliers where to
actually respond. That onward route falls into exactly one of four buckets:

``direct_portal_link``
    A clickable URL to the *specific* opportunity on a named e-sourcing portal
    (e.g. a Delta ``accessCode`` URL, a ProContract advert URL with an advert id,
    an In-tend project URL). The link identifies the opportunity, not just the
    portal home page.
``portal_generic_link``
    A clickable URL to a known portal, but only its home/login page — no
    specific-opportunity identifier.
``email_only``
    No usable onward link, but a contact email is present (``mailto:`` or an
    address in the contact text).
``none``
    Neither a usable portal link nor an email — nothing actionable found.

The portal recognition table (:data:`PORTAL_PATTERNS`) is the one place to edit
as we learn. Each entry is ``{name, url_substrings, specific_markers}``:

* ``url_substrings`` — if any appears (case-insensitively) in a URL, the URL
  belongs to this portal.
* ``specific_markers`` — if any appears, the URL carries a specific-opportunity
  identifier, so it is ``direct`` rather than ``generic``.

Portal ``name`` values mirror the ``portal_platforms`` slugs seeded in migration
0006 so the survey lines up with the rest of the project's vocabulary.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# --- Buckets ------------------------------------------------------------

BUCKET_DIRECT = "direct_portal_link"
BUCKET_GENERIC = "portal_generic_link"
BUCKET_EMAIL = "email_only"
BUCKET_NONE = "none"

#: All buckets, in report order.
BUCKETS = (BUCKET_DIRECT, BUCKET_GENERIC, BUCKET_EMAIL, BUCKET_NONE)


# --- URL / email extraction ---------------------------------------------

# Mirrors the permissive patterns used by services/portal_discovery.py so the
# survey extracts the same candidates the live discovery would. Trailing
# punctuation glued onto the end of a URL/email is stripped after the match.
URL_REGEX = re.compile(r"\bhttps?://[^\s<>\"'\)\]\}]+", re.IGNORECASE)
EMAIL_REGEX = re.compile(r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b")
_TRAILING_JUNK = ".,;:!?)]}>'\"\\"


def _dedupe(items: list[str]) -> list[str]:
    """Drop duplicates while preserving first-seen order."""
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def extract_urls(text: str | None) -> list[str]:
    """Every http(s) URL in ``text``, trailing junk stripped, de-duplicated."""
    if not text:
        return []
    return _dedupe([m.group(0).rstrip(_TRAILING_JUNK) for m in URL_REGEX.finditer(text)])


def extract_emails(text: str | None) -> list[str]:
    """Every email address in ``text`` (lower-cased), de-duplicated.

    URLs are blanked out first so an ``@`` inside a link can't masquerade as an
    email. ``mailto:`` prefixes survive because the scheme isn't http(s).
    """
    if not text:
        return []
    without_urls = URL_REGEX.sub(" ", text)
    return _dedupe(
        [m.group(0).rstrip(_TRAILING_JUNK).lower() for m in EMAIL_REGEX.finditer(without_urls)]
    )


# --- Portal recognition table -------------------------------------------


@dataclass(frozen=True)
class PortalPattern:
    """One known e-sourcing portal and how to spot a specific opportunity on it."""

    name: str
    url_substrings: tuple[str, ...]
    specific_markers: tuple[str, ...]

    def matches(self, url_lower: str) -> bool:
        return any(sub in url_lower for sub in self.url_substrings)

    def is_specific(self, url_lower: str) -> bool:
        return any(marker in url_lower for marker in self.specific_markers)


# Best-effort and intentionally easy to extend. Substrings are matched against
# the lower-cased URL (host + path + query), so both the host and any
# identifier in the query string are fair game. Markers are deliberately
# distinctive (access codes, advert/project ids) to avoid mis-labelling a portal
# home page as a specific opportunity. Edit freely as new portals show up in the
# CSV long tail.
PORTAL_PATTERNS: tuple[PortalPattern, ...] = (
    PortalPattern(
        name="delta",
        url_substrings=("delta-esourcing.com", "deltaesourcing.com"),
        specific_markers=("accesscode", "respondtolist", "noticeid=", "tenderid=", "/tenders/uk-"),
    ),
    PortalPattern(
        name="procontract",
        url_substrings=("due-north.com",),
        specific_markers=("advertid=", "projectid=", "frameworkid=", "contractid=", "/advert/"),
    ),
    PortalPattern(
        name="proactis",
        url_substrings=("proactisp2p.com", "proactis.com"),
        specific_markers=("opportunityid=", "/opportunity/", "tenderid=", "rfqid="),
    ),
    PortalPattern(
        name="jaggaer",
        url_substrings=("jaggaer.com", "bravosolution.com", "bravosolution.co.uk"),
        specific_markers=("_cmd=", "opportunityid=", "tender_id=", "objectid=", "/rfx/"),
    ),
    PortalPattern(
        name="intend",
        url_substrings=("in-tend.co.uk", "intend.co.uk", "in-tendhost.co.uk", "intendhost.co.uk"),
        specific_markers=("projectid=", "itt_id=", "/project/", "aspx?b="),
    ),
    PortalPattern(
        name="atamis",
        url_substrings=("atamis.co.uk", "atamis-"),
        specific_markers=("tenderid=", "opportunityid=", "/tender/", "/opportunity/"),
    ),
    PortalPattern(
        name="eu_supply",
        url_substrings=("eu-supply.com",),
        specific_markers=("viewnotice", "noticeid=", "appid=", "ctm/supplier"),
    ),
    PortalPattern(
        name="mytenders",
        url_substrings=(
            "mytenders.co.uk",
            "eastmidstenders.org",
            "londontenders.org",
            "supplyingthesouthwest.org.uk",
        ),
        specific_markers=("noticeid=", "show=", "advertid="),
    ),
    PortalPattern(
        name="multiquote",
        url_substrings=("multiquote.com",),
        specific_markers=("quoteid=", "rfqid=", "/quote/"),
    ),
    PortalPattern(
        name="sproc_net",
        url_substrings=("sproc.net",),
        specific_markers=("tenderid=", "/tender/", "opportunityid="),
    ),
)


def match_portal(url: str) -> PortalPattern | None:
    """Return the portal a URL belongs to, or ``None`` if it's not a known portal.

    Portal patterns use disjoint host substrings, so first-match is unambiguous.
    """
    url_lower = url.lower()
    for pattern in PORTAL_PATTERNS:
        if pattern.matches(url_lower):
            return pattern
    return None


# --- Classification -----------------------------------------------------


@dataclass(frozen=True)
class RouteClassification:
    """The verdict for one notice."""

    bucket: str
    portal: str | None  # set for direct_portal_link / portal_generic_link
    detail: str | None  # single best URL (link buckets) or email (email_only)


def classify_text(
    text: str | None, *, extra_urls: tuple[str | None, ...] = ()
) -> RouteClassification:
    """Classify a notice from a text blob.

    ``text`` is anything that may carry the onward route — description plus the
    raw notice JSON serialised to a string works well. ``extra_urls`` lets the
    caller add already-structured URLs (e.g. ``source_url``) that may not appear
    in ``text``. The first specific portal link wins; otherwise the first
    generic portal link; otherwise an email; otherwise nothing.
    """
    urls = extract_urls(text)
    urls.extend(u.rstrip(_TRAILING_JUNK) for u in extra_urls if u)
    urls = _dedupe(urls)

    direct: tuple[str, str] | None = None
    generic: tuple[str, str] | None = None
    for url in urls:
        pattern = match_portal(url)
        if pattern is None:
            continue
        if pattern.is_specific(url.lower()):
            if direct is None:
                direct = (pattern.name, url)
        elif generic is None:
            generic = (pattern.name, url)

    if direct is not None:
        return RouteClassification(BUCKET_DIRECT, direct[0], direct[1])
    if generic is not None:
        return RouteClassification(BUCKET_GENERIC, generic[0], generic[1])

    emails = extract_emails(text)
    if emails:
        return RouteClassification(BUCKET_EMAIL, None, emails[0])

    return RouteClassification(BUCKET_NONE, None, None)
