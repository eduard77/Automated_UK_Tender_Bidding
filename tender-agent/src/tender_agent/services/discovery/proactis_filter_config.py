"""Typed filter config for Proactis (procontract.due-north.com) discovery.

This is **Step 1** of the chunk-9 plan: the discovery service is driven by a
config object the operator sets in env / settings. Step 2 (later) will wire
the dashboard's main filter page to populate the same object — keeping this
file model-only means Step 2 is just plumbing, no rework of the discovery
service or its tests.

Defaults match the operator's stated intent: open opportunities only
(`include_closed = False`), no portal/organisation restrictions, generous
but bounded `max_pages`.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class ProactisFilterConfig(BaseModel):
    """Filters applied on the Find Opportunities listing.

    Field names mirror the on-screen "Narrow your results" controls so the
    Step-2 dashboard form maps 1:1. All collections default empty — an
    empty list means "don't constrain on this dimension"; the discovery
    service skips that control entirely rather than clearing a previous
    session's selections, so a leftover Proactis browser state doesn't
    accidentally drive the run.
    """

    # Free-text keyword search. Single string because the on-screen control
    # is a single text box, and Proactis treats it as one query.
    keywords: str = ""

    # Each region is added as a separate chip via the "Add new region" control.
    # Values are the labels Proactis displays (e.g. "North West", "Merseyside").
    regions: list[str] = Field(default_factory=list)

    # Each category is added separately via "Add" under Categories. Strings are
    # whatever Proactis expects (UNSPSC, CPV, ProClass labels). The discovery
    # service does not interpret these — Proactis does the lookup.
    categories: list[str] = Field(default_factory=list)

    # Optional: scope to specific portals or organisations. Empty = no
    # restriction (the dropdowns are left at their default).
    portals: list[str] = Field(default_factory=list)
    organisations: list[str] = Field(default_factory=list)

    # Open-only by default — matches the operator's "we want NO = open only".
    include_closed: bool = False

    # Pagination cap. The unfiltered listing can be 69 pages; with realistic
    # filters it's typically 1-5. The cap is a safety net, not the expected
    # value — a real run almost always ends because pagination is exhausted.
    max_pages: int = 20

    # When True, the listing walk is allowed to update an existing row's
    # last_seen_at + content_hash but does NOT open the detail page for an
    # opportunity we've already seen (by source_ref). Saves ~2-3 round-trips
    # per known opportunity. Off by default — first runs should always read
    # detail to populate `procurement_ref` for dedup.
    skip_detail_for_known: bool = False

    def is_constrained(self) -> bool:
        """True iff this config narrows the listing in some way. Useful for
        logging — an unconstrained run hits the full 69 pages."""
        return bool(
            self.keywords.strip()
            or self.regions
            or self.categories
            or self.portals
            or self.organisations
            or not self.include_closed
        )

    @classmethod
    def from_filter_profile(
        cls, profile: object, *, portals: list[str] | None = None
    ) -> ProactisFilterConfig:
        """Translate the operator's existing FilterProfile (cpv_codes +
        cpv_prefixes + regions + keywords_any) into the Proactis filter
        shape. We do NOT touch the FilterProfile model — this is a one-way
        read.

        Field mapping (Step 1):
          - keywords ← " ".join(profile.keywords_any) — Proactis's filter is
            a single text box; the operator's local AND-of-words semantics
            don't survive into the Proactis backend, but feeding the words
            is still narrower than nothing.
          - categories ← profile.cpv_codes + cpv_prefixes (deduped). The
            discovery service drives the Categories control with these; an
            unrecognised code is logged + skipped, never blocking.
          - regions ← profile.regions (as-is).
          - portals ← the `portals` argument (deployment-level scope from
            PROACTIS_DISCOVERY_PORTALS — the FilterProfile has no portals
            dimension). None/empty = the control is left alone.
          - include_closed stays False (open-only) — operator intent.

        The caller (`run_for_profile`) is the only consumer; tests assert
        the mapping is faithful for representative profile rows.
        """
        keywords_any = list(getattr(profile, "keywords_any", []) or [])
        keywords = " ".join(w for w in keywords_any if w and w.strip()).strip()
        cpv_codes = list(getattr(profile, "cpv_codes", []) or [])
        cpv_prefixes = list(getattr(profile, "cpv_prefixes", []) or [])
        # De-dup while preserving order. Codes come before prefixes because
        # the more specific entry should be tried first if Proactis offers
        # both as suggestions.
        categories: list[str] = []
        seen: set[str] = set()
        for entry in [*cpv_codes, *cpv_prefixes]:
            value = (entry or "").strip()
            if value and value not in seen:
                categories.append(value)
                seen.add(value)
        regions = [r for r in (getattr(profile, "regions", []) or []) if r and r.strip()]
        portal_names = [
            p.strip() for p in (portals or []) if p and p.strip()
        ]
        return cls(
            keywords=keywords,
            regions=regions,
            categories=categories,
            portals=portal_names,
            include_closed=False,
        )
