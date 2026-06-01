"""Go/No-Go warning engine — red warnings + scoring + self-cert + API surface.

Pure-data tests; no LLM, no network. Uses the SQLite in-memory fixtures
established by chunk 6 (`tests/_billing_fixtures.py`) so the suite runs
without Postgres.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from tender_agent.models import Tender, TenderBrief, VaultDocument, VaultDocumentVersion
from tender_agent.services.go_no_go import assess_tender
from tender_agent.services.go_no_go.engine import BriefNotReady
from tender_agent.services.go_no_go.requirements_parse import parse_requirement
from tender_agent.services.go_no_go.scoring import score_pillars
from tender_agent.services.go_no_go.warnings import (
    TIGHT_DEADLINE_WORKING_DAYS,
)
from tests._billing_fixtures import make_engine_and_session


@pytest.fixture()
def db():
    _, factory = make_engine_and_session()
    s = factory()
    try:
        yield s
    finally:
        s.close()


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


_TENDER_COUNTER = {"n": 0}


def _tender(db, *, deadline_at: datetime | None = None) -> Tender:
    _TENDER_COUNTER["n"] += 1
    t = Tender(
        source_code="TEST",
        source_ref=f"gng-{_TENDER_COUNTER['n']}",
        title="Cyber security retrofit",
        value_amount=Decimal(200_000),
        deadline_at=deadline_at,
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def _brief(
    db,
    *,
    tender_id: int,
    recommendation: str = "bid",
    confidence: str = "high",
    mandatory: list[str] | None = None,
    key_risks: list[dict] | None = None,
    deadline_iso: str | None = None,
    status: str = "complete",
) -> TenderBrief:
    body = {
        "recommendation": recommendation,
        "confidence": confidence,
        "headline": "Bid",
        "rationale": "ok",
        "key_risks": key_risks or [],
        "deadline": {"by": deadline_iso} if deadline_iso else None,
        "mandatory_requirements": mandatory or [],
        "scope_summary": "Clean retrofit, 24/7 cover.",
        "scoring": {"summary": "60/40", "criteria": []},
    }
    b = TenderBrief(
        tender_id=tender_id,
        status=status,
        recommendation=recommendation,
        confidence=confidence,
        headline="Bid",
        brief_json=body,
        generated_at=datetime.now(UTC),
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    db.add(b)
    db.commit()
    db.refresh(b)
    return b


def _vault_insurance(
    db,
    *,
    cover_amount: int | None = 10_000_000,
    insurance_type: str = "professional_indemnity",
    expiry: date | None = None,
    title: str = "PI insurance",
) -> int:
    doc = VaultDocument(
        org_id=1, category="insurance", title=title
    )
    db.add(doc)
    db.flush()
    v = VaultDocumentVersion(
        document_id=doc.id,
        version=1,
        storage_key="x",
        bytes=1,
        sha256="x",
        mime_type="application/pdf",
        title=title,
        expiry_date=expiry,
        claims={
            "doc_type": "insurance_certificate",
            "insurance_type": insurance_type,
            "cover_amount": str(cover_amount) if cover_amount is not None else None,
            "currency": "GBP",
        },
    )
    db.add(v)
    db.flush()
    doc.current_version_id = v.id
    db.commit()
    return doc.id


def _vault_accounts(
    db, *, turnover: int, fye: date = date(2025, 3, 31)
) -> int:
    doc = VaultDocument(org_id=1, category="accounts", title="Annual Accounts 2025")
    db.add(doc)
    db.flush()
    v = VaultDocumentVersion(
        document_id=doc.id,
        version=1,
        storage_key="x",
        bytes=1,
        sha256="x",
        mime_type="application/pdf",
        title="Annual Accounts 2025",
        claims={
            "doc_type": "accounts",
            "turnover": str(turnover),
            "currency": "GBP",
            "fiscal_year_end": fye.isoformat(),
        },
    )
    db.add(v)
    db.flush()
    doc.current_version_id = v.id
    db.commit()
    return doc.id


def _vault_iso(
    db,
    *,
    standard: str = "27001",
    expiry: date | None = None,
) -> int:
    doc = VaultDocument(
        org_id=1, category="certification", title=f"ISO {standard}"
    )
    db.add(doc)
    db.flush()
    v = VaultDocumentVersion(
        document_id=doc.id,
        version=1,
        storage_key="x",
        bytes=1,
        sha256="x",
        mime_type="application/pdf",
        title=f"ISO {standard}",
        expiry_date=expiry,
        claims={"doc_type": "iso_certificate", "standard": standard},
    )
    db.add(v)
    db.flush()
    doc.current_version_id = v.id
    db.commit()
    return doc.id


# ---------------------------------------------------------------------------
# parse_requirement
# ---------------------------------------------------------------------------


def test_parse_insurance_pi_with_threshold() -> None:
    p = parse_requirement("Professional Indemnity insurance £10m")
    assert p.kind == "insurance"
    assert p.insurance_type == "professional_indemnity"
    assert p.threshold_value == Decimal(10_000_000)


def test_parse_turnover_threshold() -> None:
    p = parse_requirement("Minimum £5m annual turnover")
    assert p.kind == "turnover"
    assert p.threshold_value == Decimal(5_000_000)


def test_parse_iso_standard() -> None:
    p = parse_requirement("ISO 27001 certified")
    assert p.kind == "iso_standard"
    assert p.standard == "27001"


def test_parse_cyber_essentials_plus() -> None:
    p = parse_requirement("Cyber Essentials Plus required")
    assert p.kind == "accreditation"
    assert p.standard == "cyber_essentials_plus"


def test_parse_unknown_falls_through() -> None:
    p = parse_requirement("Bid must use a tartan font")
    assert p.kind == "unknown"


# ---------------------------------------------------------------------------
# Red warnings — derived from reconciliation + brief
# ---------------------------------------------------------------------------


def test_mandatory_unmet_when_turnover_below_threshold(db) -> None:
    t = _tender(db)
    _brief(db, tender_id=t.id, mandatory=["Minimum £5m turnover"])
    _vault_accounts(db, turnover=3_000_000)
    result = assess_tender(db, t)
    warnings = [w.warning for w in result.red_warnings]
    assert "mandatory_requirement_unmet" in warnings
    # The flagged warning carries the SPECIFIC requirement text so the UI
    # can name it back to the client.
    unmet = next(
        w for w in result.red_warnings if w.warning == "mandatory_requirement_unmet"
    )
    assert "5m" in (unmet.requirement or "")


def test_mandatory_met_when_turnover_above_threshold(db) -> None:
    t = _tender(db)
    _brief(db, tender_id=t.id, mandatory=["Minimum £5m turnover"])
    _vault_accounts(db, turnover=8_000_000)
    result = assess_tender(db, t)
    assert all(
        w.warning != "mandatory_requirement_unmet" for w in result.red_warnings
    )


def test_unknown_data_emits_please_confirm_not_a_warning(db) -> None:
    """No vault evidence at all → please_confirm, NOT mandatory_requirement_unmet."""
    t = _tender(db)
    _brief(db, tender_id=t.id, mandatory=["Minimum £5m turnover"])
    result = assess_tender(db, t)
    assert all(
        w.warning != "mandatory_requirement_unmet" for w in result.red_warnings
    )
    # Surfaced as missing_info to ask the client to confirm.
    reasons = " ".join(m.reason for m in result.missing_info)
    assert "5m" in reasons or "turnover" in reasons.lower()


def test_insurance_shortfall_emits_warning(db) -> None:
    t = _tender(db)
    _brief(db, tender_id=t.id, mandatory=["£10m Professional Indemnity insurance"])
    _vault_insurance(db, cover_amount=5_000_000)
    result = assess_tender(db, t)
    assert any(w.warning == "mandatory_requirement_unmet" for w in result.red_warnings)
    assert any(
        rw.kind == "shortfall" for rw in result.reconciliation.warnings
    )


def test_expiry_warning_when_cert_lapses_before_contract_end(db) -> None:
    t = _tender(db)
    t.contract_end = date.today() + timedelta(days=365)
    db.commit()
    _brief(db, tender_id=t.id, mandatory=["ISO 27001 certified"])
    _vault_iso(db, standard="27001", expiry=date.today() + timedelta(days=30))
    result = assess_tender(db, t)
    assert any(rw.kind == "expiry" for rw in result.reconciliation.warnings)
    # Expiry rolls up into the red warnings as mandatory_requirement_unmet.
    assert any(
        w.warning == "mandatory_requirement_unmet" for w in result.red_warnings
    )


def test_tight_deadline_fires_when_inside_threshold(db) -> None:
    soon = datetime.now(UTC) + timedelta(days=2)
    t = _tender(db, deadline_at=soon)
    _brief(db, tender_id=t.id, mandatory=[])
    result = assess_tender(db, t)
    assert any(w.warning == "tight_deadline" for w in result.red_warnings)
    assert (
        result.working_days_remaining is not None
        and result.working_days_remaining <= TIGHT_DEADLINE_WORKING_DAYS
    )


def test_tight_deadline_does_not_fire_for_distant_deadline(db) -> None:
    later = datetime.now(UTC) + timedelta(days=60)
    t = _tender(db, deadline_at=later)
    _brief(db, tender_id=t.id)
    result = assess_tender(db, t)
    assert all(w.warning != "tight_deadline" for w in result.red_warnings)


def test_missing_deadline_yields_missing_info_not_warning(db) -> None:
    t = _tender(db, deadline_at=None)
    _brief(db, tender_id=t.id, deadline_iso=None)
    result = assess_tender(db, t)
    assert all(w.warning != "tight_deadline" for w in result.red_warnings)
    assert any(m.criterion == "submission_deadline" for m in result.missing_info)


def test_likely_unprofitable_only_when_brief_flags_it(db) -> None:
    t = _tender(db)
    _brief(db, tender_id=t.id)
    no_flag = assess_tender(db, t)
    assert all(w.warning != "likely_unprofitable" for w in no_flag.red_warnings)
    # Now with an explicit signal in key_risks.
    t2 = _tender(db)
    _brief(
        db,
        tender_id=t2.id,
        key_risks=[
            {
                "title": "Likely unprofitable",
                "detail": "Buyer's likely price floor sits below our cost base.",
            }
        ],
    )
    flagged = assess_tender(db, t2)
    assert any(w.warning == "likely_unprofitable" for w in flagged.red_warnings)


def test_cannot_deliver_to_standard_only_when_brief_flags_it(db) -> None:
    t = _tender(db)
    _brief(
        db,
        tender_id=t.id,
        key_risks=[
            {
                "title": "Capacity gap",
                "detail": "Insufficient capacity to deliver to 24/7 standard.",
            }
        ],
    )
    result = assess_tender(db, t)
    assert any(
        w.warning == "cannot_deliver_to_standard" for w in result.red_warnings
    )


# ---------------------------------------------------------------------------
# Scoring / band / confidence
# ---------------------------------------------------------------------------


def test_scoring_returns_a_band_and_confidence(db) -> None:
    t = _tender(db)
    _brief(db, tender_id=t.id, recommendation="bid", confidence="high")
    result = assess_tender(db, t)
    assert result.rating.recommendation in {"go", "conditional", "no_go"}
    assert result.rating.confidence in {"high", "medium", "low"}


def test_band_is_no_go_when_a_red_warning_fires(db) -> None:
    t = _tender(db, deadline_at=datetime.now(UTC) + timedelta(days=1))
    _brief(db, tender_id=t.id, recommendation="bid")
    result = assess_tender(db, t)
    assert result.red_warnings
    # Spec: a red warning is enough to put the band at no_go (advisory only).
    assert result.rating.recommendation == "no_go"


def test_confidence_is_low_when_no_pricing_history() -> None:
    """We have no BidPricingHistory table; spec says low confidence in that case."""
    from tender_agent.models import TenderBrief

    brief = TenderBrief(
        tender_id=1,
        status="complete",
        recommendation="bid",
        confidence="high",
        brief_json={"recommendation": "bid", "confidence": "high"},
    )
    rating = score_pillars(brief=brief, has_red_warning=False)
    assert rating.confidence == "low"


def test_band_is_never_returned_as_block(db) -> None:
    """Confirm the API never emits 'block' or 'refuse' — the spec's whole point."""
    t = _tender(db)
    _brief(db, tender_id=t.id)
    result = assess_tender(db, t)
    body = result.to_dict()
    assert body["recommendation"] in {"go", "conditional", "no_go"}
    assert "block" not in body["recommendation"]


# ---------------------------------------------------------------------------
# Self-certification questions
# ---------------------------------------------------------------------------


def test_three_self_certification_questions_always_emitted(db) -> None:
    t = _tender(db)
    _brief(db, tender_id=t.id)
    result = assess_tender(db, t)
    ids = [q.id for q in result.self_certification]
    assert ids == ["has_documents", "qualifies", "accepts_proceed"]


def test_qualifies_question_prefilled_when_mandatory_flagged(db) -> None:
    t = _tender(db)
    _brief(
        db,
        tender_id=t.id,
        mandatory=["Minimum £5m turnover"],
    )
    _vault_accounts(db, turnover=3_000_000)
    result = assess_tender(db, t)
    qualifies = next(q for q in result.self_certification if q.id == "qualifies")
    assert qualifies.prefill is not None
    flagged = qualifies.prefill["flagged_requirements"]
    assert flagged and "5m" in flagged[0]["requirement"]
    # And the related_warnings list mirrors the red warnings shown.
    assert qualifies.related_warnings


def test_qualifies_question_not_prefilled_when_no_red_warning(db) -> None:
    t = _tender(db)
    _brief(db, tender_id=t.id)
    result = assess_tender(db, t)
    qualifies = next(q for q in result.self_certification if q.id == "qualifies")
    assert qualifies.prefill is None


# ---------------------------------------------------------------------------
# No-brief state
# ---------------------------------------------------------------------------


def test_no_brief_yet_raises_brief_not_ready(db) -> None:
    t = _tender(db)
    with pytest.raises(BriefNotReady):
        assess_tender(db, t)


def test_failed_brief_also_treated_as_not_ready(db) -> None:
    t = _tender(db)
    _brief(db, tender_id=t.id, status="failed")
    with pytest.raises(BriefNotReady):
        assess_tender(db, t)
