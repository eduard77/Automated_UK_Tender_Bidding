"""Cross-source duplicate detection.

Same tender often appears on FTS and Contracts Finder simultaneously. We try to
detect this and link records via Tender.duplicate_of_id, keeping the original
(first-seen) as canonical.

Strategy:
1. Strong match: identical procurement_ref AND fuzzy buyer name match
2. Probable match: fuzzy title (>=92) + same buyer + value within 5%
"""
from __future__ import annotations

from rapidfuzz import fuzz
from sqlalchemy import select
from sqlalchemy.orm import Session

from tender_agent.models import Tender
from tender_agent.schemas import NormalisedTender


def _norm(s: str | None) -> str:
    return (s or "").strip().lower()


def find_duplicate(db: Session, candidate: NormalisedTender) -> Tender | None:
    """Return an existing Tender that this normalised one duplicates, or None."""
    # 1. Strong match by procurement_ref
    if candidate.procurement_ref:
        rows = db.execute(
            select(Tender).where(
                Tender.procurement_ref == candidate.procurement_ref,
                Tender.source_code != candidate.source_code,
            )
        ).scalars()
        for existing in rows:
            if _buyer_match(existing.buyer_name, candidate.buyer_name):
                return existing

    # 2. Fuzzy by buyer + title + value
    if not candidate.buyer_name or not candidate.title:
        return None

    rows = db.execute(
        select(Tender)
        .where(
            Tender.source_code != candidate.source_code,
            Tender.buyer_name.isnot(None),
        )
        .order_by(Tender.first_seen_at.desc())
        .limit(500)  # bounded scan; for production, narrow by buyer_name index
    ).scalars()

    for existing in rows:
        if not _buyer_match(existing.buyer_name, candidate.buyer_name):
            continue
        title_score = fuzz.token_set_ratio(_norm(existing.title), _norm(candidate.title))
        if title_score < 92:
            continue
        if not _value_close(existing.value_amount, candidate.value_amount):
            continue
        return existing

    return None


def _buyer_match(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    return fuzz.token_set_ratio(_norm(a), _norm(b)) >= 90


def _value_close(a, b) -> bool:
    if a is None or b is None:
        return True  # don't disqualify on missing value
    if a == 0 and b == 0:
        return True
    if a == 0 or b == 0:
        return False
    diff = abs(float(a) - float(b)) / max(float(a), float(b))
    return diff <= 0.05
