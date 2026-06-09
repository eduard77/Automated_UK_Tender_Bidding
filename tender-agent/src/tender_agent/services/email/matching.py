"""Match an email to a tender by EXACT reference in the subject line.

The rule (PROJECT.md §5.8, this feature's spec): extract reference-shaped tokens
from the subject and match them EXACTLY (case-insensitive, whole-token) against
the references the tender records hold — ``source_ref`` and ``procurement_ref``.

NO fuzzy matching, NO guessing from buyer + subject. If no exact reference
match, the email is left alone. Odd formats (``DN 12345`` split by a space,
``DN12345X`` with an extra char) are missed by design — the safe trade-off.

A token that *looks* like a reference but matches no tender we hold is returned
in ``unmatched_ref_shaped`` so the caller can LOG the miss (no other action).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from tender_agent.models import Tender

# A reference-shaped token: starts alphanumeric, then alphanumerics or the
# separators references commonly use (-, _, /). Length >= 3 overall. We stop at
# whitespace and sentence punctuation, so "DN12345." yields "DN12345".
_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-_/]{2,}")


@dataclass
class SubjectMatch:
    tender_id: int | None
    matched_ref: str | None
    candidates: list[str] = field(default_factory=list)
    # Ref-shaped tokens that matched no tender — logged, never actioned.
    unmatched_ref_shaped: list[str] = field(default_factory=list)


def extract_candidates(subject: str) -> list[str]:
    """Reference-shaped tokens from a subject, de-duplicated, order preserved."""
    seen: set[str] = set()
    out: list[str] = []
    for tok in _TOKEN_RE.findall(subject or ""):
        if tok not in seen:
            seen.add(tok)
            out.append(tok)
    return out


def _looks_like_reference(token: str) -> bool:
    """Heuristic used ONLY for logging misses: a token with a digit and some
    length is plausibly a reference. Never affects matching."""
    return len(token) >= 5 and any(c.isdigit() for c in token)


def match_subject_to_tender(db: Session, subject: str) -> SubjectMatch:
    """Resolve a subject to at most one tender via exact reference.

    Returns the first candidate (in subject order) that exactly equals a known
    ``source_ref`` or ``procurement_ref``. If none match, reports ref-shaped
    tokens for logging.
    """
    candidates = extract_candidates(subject)
    if not candidates:
        return SubjectMatch(tender_id=None, matched_ref=None)

    # Case-insensitive exact equality of the WHOLE token to the WHOLE reference.
    norm_to_raw: dict[str, str] = {}
    for c in candidates:
        norm_to_raw.setdefault(c.upper(), c)
    norms = list(norm_to_raw)

    rows = db.execute(
        select(Tender.id, Tender.source_ref, Tender.procurement_ref).where(
            or_(
                func.upper(Tender.source_ref).in_(norms),
                func.upper(Tender.procurement_ref).in_(norms),
            )
        )
    ).all()

    known: dict[str, int] = {}
    for tender_id, source_ref, procurement_ref in rows:
        for ref in (source_ref, procurement_ref):
            if ref and ref.upper() in norm_to_raw:
                known.setdefault(ref.upper(), tender_id)

    # Pick by subject order so the first reference in the subject wins.
    for c in candidates:
        key = c.upper()
        if key in known:
            return SubjectMatch(
                tender_id=known[key],
                matched_ref=norm_to_raw[key],
                candidates=candidates,
            )

    return SubjectMatch(
        tender_id=None,
        matched_ref=None,
        candidates=candidates,
        unmatched_ref_shaped=[c for c in candidates if _looks_like_reference(c)],
    )
