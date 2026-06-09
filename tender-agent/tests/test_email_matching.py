"""Exact subject-reference matching.

EXACT match only: a reference-shaped token in the subject must equal a known
``source_ref`` or ``procurement_ref`` in full (case-insensitive). Odd formats
and near-misses are NOT matched, by design. A ref-shaped token with no tender is
reported for logging but never actioned.
"""
from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from tender_agent.services.email.matching import (
    extract_candidates,
    match_subject_to_tender,
)
from tests._billing_fixtures import make_engine_and_session
from tests._email_fixtures import make_tender


@pytest.fixture()
def db() -> Session:
    _, factory = make_engine_and_session()
    s = factory()
    try:
        yield s
    finally:
        s.close()


def test_exact_source_ref_in_subject_matches(db: Session) -> None:
    tender = make_tender(db, source_ref="DN12345")
    result = match_subject_to_tender(db, "RE: Clarification on DN12345 - please read")
    assert result.tender_id == tender.id
    assert result.matched_ref == "DN12345"


def test_exact_procurement_ref_matches(db: Session) -> None:
    tender = make_tender(db, source_ref="abc-1", procurement_ref="PROC-2026-0099")
    result = match_subject_to_tender(db, "Tender PROC-2026-0099 documents published")
    assert result.tender_id == tender.id
    assert result.matched_ref == "PROC-2026-0099"


def test_match_is_case_insensitive(db: Session) -> None:
    tender = make_tender(db, source_ref="DN12345")
    result = match_subject_to_tender(db, "update: dn12345 amended")
    assert result.tender_id == tender.id


def test_near_miss_extra_char_does_not_match(db: Session) -> None:
    make_tender(db, source_ref="DN12345")
    # An extra trailing character makes a different whole token — no match.
    result = match_subject_to_tender(db, "About DN12345X today")
    assert result.tender_id is None


def test_space_split_reference_does_not_match(db: Session) -> None:
    make_tender(db, source_ref="DN12345")
    # "DN 12345" tokenises to "DN" and "12345"; neither equals "DN12345".
    result = match_subject_to_tender(db, "Notice DN 12345 issued")
    assert result.tender_id is None


def test_reference_shaped_token_with_no_tender_is_logged_not_actioned(
    db: Session,
) -> None:
    make_tender(db, source_ref="DN12345")
    result = match_subject_to_tender(db, "Invoice REF-99887 attached")
    assert result.tender_id is None
    # Reported for logging (a digits-bearing token), but no action taken.
    assert "REF-99887" in result.unmatched_ref_shaped


def test_subject_with_no_candidates(db: Session) -> None:
    make_tender(db, source_ref="DN12345")
    result = match_subject_to_tender(db, "hi")
    assert result.tender_id is None
    assert result.candidates == []


def test_extract_candidates_dedupes_and_preserves_order() -> None:
    # The tokeniser is intentionally broad (ordinary words come through too);
    # the EXACT-match step is what filters to real references. It must dedupe
    # repeats and preserve first-seen order.
    cands = extract_candidates("DN12345 and DN12345 then ABC-9")
    assert cands.count("DN12345") == 1
    assert "ABC-9" in cands
    assert cands.index("DN12345") < cands.index("ABC-9")
