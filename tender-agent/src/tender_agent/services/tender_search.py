"""Source-agnostic search over the canonical tender table (Phase 4 chunk 7).

Everything here filters the common Tender shape — title, description, keywords,
cpv_codes, buyer fields, value band, dates, status, source_code, and the dedup
columns. Nothing is hardcoded to a particular source, so when Find a Tender /
Proactis / Delta tenders land they are searchable and source-filterable with no
change to this module.

The query is assembled once as a list of WHERE clauses and reused for both the
COUNT and the page SELECT so the total and the results never disagree.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import ColumnElement, Select, and_, func, or_, select
from sqlalchemy.orm import Session

from tender_agent.models import Tender
from tender_agent.services.regions import CANONICAL_REGIONS

# Status value that represents an "open"/live notice. The table uses
# active/planned/closed/NULL; "open only" means a live notice still accepting
# responses (active + a future deadline).
OPEN_STATUS = "active"

MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 25


class SortKey(StrEnum):
    deadline_asc = "deadline_asc"  # closing soonest (default)
    published_desc = "published_desc"  # newest
    value_desc = "value_desc"  # highest value


@dataclass(slots=True)
class TenderSearchParams:
    """Parsed, validated search criteria. All fields optional; combined with AND
    across fields and OR within a multi-valued field."""

    q: str | None = None
    cpv: list[str] = field(default_factory=list)
    region: list[str] = field(default_factory=list)
    buyer: str | None = None
    value_min: Decimal | None = None
    value_max: Decimal | None = None
    # When a value range is set, null-value tenders are INCLUDED by default
    # (828/3308 of the table has no stated value). Flip this on to exclude them.
    value_stated_only: bool = False
    deadline_from: datetime | None = None
    deadline_to: datetime | None = None
    open_only: bool = False
    status: list[str] = field(default_factory=list)
    source: list[str] = field(default_factory=list)
    include_duplicates: bool = True
    sort: SortKey = SortKey.deadline_asc
    page: int = 1
    page_size: int = DEFAULT_PAGE_SIZE


def _like(term: str) -> str:
    """Escape LIKE wildcards in a user term and wrap it for a contains-match."""
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _build_conditions(params: TenderSearchParams) -> list[ColumnElement[bool]]:
    conditions: list[ColumnElement[bool]] = []

    # Free text — case-insensitive contains across title, description, keywords.
    # keywords is a text[]; array_to_string lets a single ILIKE span every tag.
    if params.q and params.q.strip():
        pat = _like(params.q.strip())
        conditions.append(
            or_(
                Tender.title.ilike(pat),
                Tender.description.ilike(pat),
                func.array_to_string(Tender.keywords, " ").ilike(pat),
            )
        )

    # CPV — match if any requested code overlaps the tender's cpv_codes array.
    if params.cpv:
        conditions.append(Tender.cpv_codes.overlap(params.cpv))

    # Region — exact match against the canonical `region` column (chunk 8),
    # multi-select OR. buyer_region is messy free text and is no longer the
    # filter target; `region` is the normalised, canonical value.
    if params.region:
        conditions.append(Tender.region.in_(params.region))

    if params.buyer and params.buyer.strip():
        conditions.append(Tender.buyer_name.ilike(_like(params.buyer.strip())))

    # Value range. A tender's band is [low, high] where low/high fall back to
    # value_amount. The requested [value_min, value_max] (either side optional)
    # matches when the bands overlap.
    #
    # Null-value tenders (no value_amount/min/max at all) are a large share of
    # the table and are INCLUDED by default so a value range never silently
    # drops them — they surface marked "Value not stated". The
    # `value_stated_only` toggle excludes them.
    if params.value_min is not None or params.value_max is not None:
        low = func.coalesce(Tender.value_min, Tender.value_amount)
        high = func.coalesce(Tender.value_max, Tender.value_amount)
        has_value = or_(
            Tender.value_amount.isnot(None),
            Tender.value_min.isnot(None),
            Tender.value_max.isnot(None),
        )
        range_clauses: list[ColumnElement[bool]] = [has_value]
        if params.value_min is not None:
            range_clauses.append(high >= params.value_min)
        if params.value_max is not None:
            range_clauses.append(low <= params.value_max)
        in_range = and_(*range_clauses)
        if params.value_stated_only:
            conditions.append(in_range)
        else:
            no_value = and_(
                Tender.value_amount.is_(None),
                Tender.value_min.is_(None),
                Tender.value_max.is_(None),
            )
            conditions.append(or_(in_range, no_value))

    # Deadline window + "open only" convenience.
    if params.deadline_from is not None:
        conditions.append(Tender.deadline_at >= params.deadline_from)
    if params.deadline_to is not None:
        conditions.append(Tender.deadline_at <= params.deadline_to)
    if params.open_only:
        conditions.append(Tender.deadline_at >= datetime.now(UTC))
        conditions.append(Tender.status == OPEN_STATUS)

    if params.status:
        conditions.append(Tender.status.in_(params.status))

    if params.source:
        conditions.append(Tender.source_code.in_(params.source))

    # Dedup: default shows everything (annotated downstream); when off, only
    # primaries (rows that are not a duplicate of another).
    if not params.include_duplicates:
        conditions.append(Tender.duplicate_of_id.is_(None))

    return conditions


def _apply_sort(stmt: Select, sort: SortKey) -> Select:
    if sort is SortKey.published_desc:
        return stmt.order_by(Tender.published_at.desc().nullslast(), Tender.id.desc())
    if sort is SortKey.value_desc:
        value = func.coalesce(Tender.value_amount, Tender.value_max, Tender.value_min)
        return stmt.order_by(value.desc().nullslast(), Tender.id.desc())
    # deadline_asc — closing soonest first; tenders without a deadline go last.
    return stmt.order_by(Tender.deadline_at.asc().nullslast(), Tender.id.asc())


def search_tenders(db: Session, params: TenderSearchParams) -> tuple[int, list[Tender]]:
    """Run a search, returning (total_matches, page_of_tenders).

    `total` is the full match count irrespective of pagination; the returned
    list is the requested page.
    """
    conditions = _build_conditions(params)

    total = db.execute(
        select(func.count()).select_from(Tender).where(*conditions)
    ).scalar_one()

    page = max(params.page, 1)
    page_size = max(1, min(params.page_size, MAX_PAGE_SIZE))

    stmt = _apply_sort(select(Tender).where(*conditions), params.sort)
    stmt = stmt.offset((page - 1) * page_size).limit(page_size)
    rows = list(db.execute(stmt).scalars().all())
    return total, rows


_CANONICAL_ORDER = {r: i for i, r in enumerate(CANONICAL_REGIONS)}


def tender_facets(
    db: Session,
) -> tuple[list[str], list[str], list[tuple[str, int]]]:
    """Distinct sources/statuses and canonical regions (with counts) present in
    the table — drives the filter controls and stays correct as new
    sources/regions are ingested.

    Regions come from the normalised `region` column (chunk 8): each present
    canonical region with how many tenders carry it, ordered by the canonical
    list so the dropdown reads North → South, UK-wide first."""
    sources = [
        s
        for (s,) in db.execute(
            select(Tender.source_code).distinct().order_by(Tender.source_code)
        ).all()
        if s
    ]
    statuses = [
        s
        for (s,) in db.execute(
            select(Tender.status).distinct().order_by(Tender.status)
        ).all()
        if s
    ]
    region_rows = db.execute(
        select(Tender.region, func.count())
        .where(Tender.region.isnot(None))
        .group_by(Tender.region)
    ).all()
    regions = sorted(
        ((r, int(c)) for (r, c) in region_rows if r),
        key=lambda rc: (_CANONICAL_ORDER.get(rc[0], len(_CANONICAL_ORDER)), rc[0]),
    )
    return sources, statuses, regions
