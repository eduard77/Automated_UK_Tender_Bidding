"""Vault reconciliation — word vs evidence (`docs/bid-writing/go-no-go.yaml`).

Asserts:
- £3m turnover vs £5m required → contradiction with the source document.
- Insurance shortfall → shortfall row with required + evidenced values.
- ISO cert expiring before contract end → expiry row.
- No vault evidence for a known requirement → please_confirm, NEVER a
  warning (the spec's "do NOT assert unmet on unknown" rule).
- A later upload that meets the threshold → contradiction stops being
  emitted (re-runnable on demand).
- Unparsable requirement → please_confirm + unparsed_requirements entry.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from tender_agent.models import (
    Tender,
    TenderBrief,
    VaultDocument,
    VaultDocumentVersion,
)
from tender_agent.services.go_no_go import reconcile_vault_against_tender
from tests._billing_fixtures import make_engine_and_session


@pytest.fixture()
def db():
    _, factory = make_engine_and_session()
    s = factory()
    try:
        yield s
    finally:
        s.close()


_COUNTER = {"n": 0}


def _tender(
    db,
    *,
    contract_end: date | None = None,
    deadline_at: datetime | None = None,
) -> Tender:
    _COUNTER["n"] += 1
    t = Tender(
        source_code="TEST",
        source_ref=f"rec-{_COUNTER['n']}",
        title="A tender",
        value_amount=Decimal(200_000),
        deadline_at=deadline_at,
        contract_end=contract_end,
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def _brief(db, *, tender_id: int, mandatory: list[str]) -> TenderBrief:
    b = TenderBrief(
        tender_id=tender_id,
        status="complete",
        recommendation="bid",
        confidence="medium",
        brief_json={
            "recommendation": "bid",
            "mandatory_requirements": mandatory,
        },
        generated_at=datetime.now(UTC),
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    db.add(b)
    db.commit()
    db.refresh(b)
    return b


def _vault_doc(
    db,
    *,
    category: str,
    title: str,
    claims: dict,
    expiry: date | None = None,
) -> int:
    doc = VaultDocument(org_id=1, category=category, title=title)
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
        claims=claims,
    )
    db.add(v)
    db.flush()
    doc.current_version_id = v.id
    db.commit()
    return doc.id


# ---------------------------------------------------------------------------
# Contradictions / shortfalls
# ---------------------------------------------------------------------------


def test_turnover_3m_vs_5m_emits_contradiction_with_source(db) -> None:
    t = _tender(db)
    brief = _brief(db, tender_id=t.id, mandatory=["Minimum £5m turnover"])
    _vault_doc(
        db,
        category="accounts",
        title="Annual Accounts 2025",
        claims={
            "doc_type": "accounts",
            "turnover": "3000000",
            "currency": "GBP",
            "fiscal_year_end": "2025-03-31",
        },
    )
    result = reconcile_vault_against_tender(db, tender=t, brief=brief)
    contradictions = [w for w in result.warnings if w.kind == "contradiction"]
    assert len(contradictions) == 1
    w = contradictions[0]
    assert w.evidenced_value and "3m" in w.evidenced_value
    assert w.required_value and "5m" in w.required_value
    assert w.source_document is not None
    assert "Annual Accounts" in w.source_document["title"]


def test_insurance_shortfall_emits_shortfall_with_values(db) -> None:
    t = _tender(db)
    brief = _brief(
        db,
        tender_id=t.id,
        mandatory=["£10m Professional Indemnity insurance"],
    )
    _vault_doc(
        db,
        category="insurance",
        title="PI 2025",
        claims={
            "doc_type": "insurance_certificate",
            "insurance_type": "professional_indemnity",
            "cover_amount": "5000000",
            "currency": "GBP",
        },
    )
    result = reconcile_vault_against_tender(db, tender=t, brief=brief)
    shortfalls = [w for w in result.warnings if w.kind == "shortfall"]
    assert len(shortfalls) == 1
    assert "10m" in shortfalls[0].required_value
    assert "5m" in shortfalls[0].evidenced_value


# ---------------------------------------------------------------------------
# Expiry
# ---------------------------------------------------------------------------


def test_expiry_emitted_when_iso_lapses_before_contract_end(db) -> None:
    contract_end = date.today() + timedelta(days=365)
    t = _tender(db, contract_end=contract_end)
    brief = _brief(db, tender_id=t.id, mandatory=["ISO 27001"])
    _vault_doc(
        db,
        category="certification",
        title="ISO 27001 cert",
        claims={"doc_type": "iso_certificate", "standard": "27001"},
        expiry=date.today() + timedelta(days=30),
    )
    result = reconcile_vault_against_tender(db, tender=t, brief=brief)
    expiries = [w for w in result.warnings if w.kind == "expiry"]
    assert len(expiries) == 1
    assert expiries[0].expiry_date is not None
    assert expiries[0].contract_end == contract_end


def test_no_expiry_when_cert_outlasts_contract(db) -> None:
    contract_end = date.today() + timedelta(days=30)
    t = _tender(db, contract_end=contract_end)
    brief = _brief(db, tender_id=t.id, mandatory=["ISO 27001"])
    _vault_doc(
        db,
        category="certification",
        title="ISO 27001 cert",
        claims={"doc_type": "iso_certificate", "standard": "27001"},
        expiry=date.today() + timedelta(days=400),
    )
    result = reconcile_vault_against_tender(db, tender=t, brief=brief)
    assert not [w for w in result.warnings if w.kind == "expiry"]


# ---------------------------------------------------------------------------
# please_confirm — unknown evidence, never a fabricated warning
# ---------------------------------------------------------------------------


def test_no_evidence_emits_please_confirm_not_warning(db) -> None:
    t = _tender(db)
    brief = _brief(db, tender_id=t.id, mandatory=["Minimum £5m turnover"])
    # No vault rows at all.
    result = reconcile_vault_against_tender(db, tender=t, brief=brief)
    assert not result.warnings  # no warnings fabricated
    assert any(p.requirement.endswith("turnover") for p in result.please_confirm)


def test_insurance_without_extracted_cover_emits_please_confirm(db) -> None:
    t = _tender(db)
    brief = _brief(
        db, tender_id=t.id, mandatory=["£10m Professional Indemnity insurance"]
    )
    _vault_doc(
        db,
        category="insurance",
        title="PI cert (cover not extracted)",
        claims={
            "doc_type": "insurance_certificate",
            "insurance_type": "professional_indemnity",
            "cover_amount": None,
        },
    )
    result = reconcile_vault_against_tender(db, tender=t, brief=brief)
    assert not result.warnings
    assert result.please_confirm


def test_unparsed_requirement_recorded_and_please_confirm(db) -> None:
    t = _tender(db)
    brief = _brief(
        db,
        tender_id=t.id,
        mandatory=["Bid must be presented in a Tartan font"],
    )
    result = reconcile_vault_against_tender(db, tender=t, brief=brief)
    assert (
        "Bid must be presented in a Tartan font" in result.unparsed_requirements
    )
    assert any("Tartan" in p.requirement for p in result.please_confirm)


# ---------------------------------------------------------------------------
# Re-runnable: later evidence clears contradictions
# ---------------------------------------------------------------------------


def test_later_evidence_clears_contradiction(db) -> None:
    t = _tender(db)
    brief = _brief(db, tender_id=t.id, mandatory=["Minimum £5m turnover"])
    # First, evidence shows £3m → contradiction.
    doc = VaultDocument(
        org_id=1, category="accounts", title="Annual Accounts 2024"
    )
    db.add(doc)
    db.flush()
    v_old = VaultDocumentVersion(
        document_id=doc.id,
        version=1,
        storage_key="x",
        bytes=1,
        sha256="x",
        mime_type="application/pdf",
        title="Annual Accounts 2024",
        claims={
            "doc_type": "accounts",
            "turnover": "3000000",
            "fiscal_year_end": "2024-03-31",
        },
    )
    db.add(v_old)
    db.flush()
    doc.current_version_id = v_old.id
    db.commit()

    first = reconcile_vault_against_tender(db, tender=t, brief=brief)
    assert any(w.kind == "contradiction" for w in first.warnings)

    # Now they upload 2025 accounts with £8m turnover (supersede via
    # current_version_id swap — the spec model). The reconciliation simply
    # re-runs and the contradiction disappears.
    v_new = VaultDocumentVersion(
        document_id=doc.id,
        version=2,
        storage_key="y",
        bytes=1,
        sha256="y",
        mime_type="application/pdf",
        title="Annual Accounts 2025",
        claims={
            "doc_type": "accounts",
            "turnover": "8000000",
            "fiscal_year_end": "2025-03-31",
        },
    )
    db.add(v_new)
    db.flush()
    v_old.superseded_by_version_id = v_new.id
    doc.current_version_id = v_new.id
    db.commit()

    second = reconcile_vault_against_tender(db, tender=t, brief=brief)
    assert not [w for w in second.warnings if w.kind == "contradiction"]


def test_reconciliation_skips_other_tenant(db) -> None:
    t = _tender(db)
    brief = _brief(db, tender_id=t.id, mandatory=["Minimum £5m turnover"])
    # Accounts row belongs to a DIFFERENT tenant — must be invisible.
    doc = VaultDocument(org_id=999, category="accounts", title="Other tenant")
    db.add(doc)
    db.flush()
    v = VaultDocumentVersion(
        document_id=doc.id,
        version=1,
        storage_key="z",
        bytes=1,
        sha256="z",
        mime_type="application/pdf",
        title="Other tenant",
        claims={
            "doc_type": "accounts",
            "turnover": "8000000",
            "fiscal_year_end": "2025-03-31",
        },
    )
    db.add(v)
    db.flush()
    doc.current_version_id = v.id
    db.commit()
    result = reconcile_vault_against_tender(db, tender=t, brief=brief, org_id=1)
    # Cross-tenant inference must not satisfy the requirement — we should
    # ask the client to confirm.
    assert any(
        "turnover" in p.requirement.lower() for p in result.please_confirm
    )
    assert not any(w.kind == "contradiction" for w in result.warnings)
