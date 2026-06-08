"""Scan + summarise CF/FTS onward routes (operator diagnostic).

READ-ONLY. This is the shared scan/aggregation layer used by BOTH the CLI
(``scripts/survey_cf_onward_routes.py``) and the
``GET /admin/diagnostics/cf-onward-routes`` API endpoint, so the database
selection and counting live in exactly one place. The classification itself is
in :mod:`tender_agent.diagnostics.onward_routes` and is not duplicated here —
this module only assembles the text to classify and tallies the verdicts.

No writes, no schema changes, no network calls.
"""
from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from tender_agent.diagnostics.onward_routes import (
    BUCKET_DIRECT,
    BUCKETS,
    RouteClassification,
    classify_text,
)
from tender_agent.models import Tender

#: Sources the survey scans by default — Contracts Finder + Find a Tender.
DEFAULT_SOURCES: tuple[str, ...] = ("CF", "FTS")


def tender_blob(tender: Tender) -> tuple[str, tuple[str | None, ...]]:
    """Assemble the text blob + extra URLs to classify one tender from.

    The raw OCDS release is serialised wholesale so contact points, submission
    details, and document links are all in scope without hard-coding paths.
    """
    parts: list[str] = []
    if tender.description:
        parts.append(tender.description)
    if tender.raw is not None:
        parts.append(json.dumps(tender.raw, default=str))
    if tender.documents:
        parts.append(json.dumps(tender.documents, default=str))
    return "\n".join(parts), (tender.source_url,)


def classify_tender(tender: Tender) -> RouteClassification:
    """Classify a single tender's onward route from its stored fields."""
    text, extra_urls = tender_blob(tender)
    return classify_text(text, extra_urls=extra_urls)


def iter_classified(
    db: Session,
    sources: tuple[str, ...] = DEFAULT_SOURCES,
    limit: int | None = None,
) -> Iterator[tuple[Tender, RouteClassification]]:
    """Yield ``(tender, classification)`` for each scanned tender, ordered by id.

    Read-only: a single ``SELECT`` over the chosen source codes.
    """
    stmt = select(Tender).where(Tender.source_code.in_(tuple(sources))).order_by(Tender.id)
    if limit is not None:
        stmt = stmt.limit(limit)
    for tender in db.execute(stmt).scalars():
        yield tender, classify_tender(tender)


@dataclass(frozen=True)
class SurveySummary:
    """Aggregate counts for one survey run."""

    total_scanned: int
    #: Count per bucket, keyed by the four bucket names, always all four present.
    buckets: dict[str, int]
    #: (portal_name, count) within direct_portal_link, sorted by count desc.
    direct_portal_breakdown: list[tuple[str, int]]


def summarise(results: list[tuple[Tender, RouteClassification]]) -> SurveySummary:
    """Tally bucket counts and the per-portal direct-link breakdown."""
    bucket_counts: Counter[str] = Counter(cls.bucket for _, cls in results)
    portal_counts: Counter[str] = Counter(
        cls.portal for _, cls in results if cls.bucket == BUCKET_DIRECT and cls.portal
    )
    return SurveySummary(
        total_scanned=len(results),
        buckets={bucket: bucket_counts.get(bucket, 0) for bucket in BUCKETS},
        direct_portal_breakdown=portal_counts.most_common(),
    )
