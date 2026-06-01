"""Read the vault (tenant-scoped) and project it into the shape the
reconciliation engine needs.

We pull `current_version` per VaultDocument so superseded rows are skipped
naturally — versioning is the source of truth for "what is in effect now".
All filters are `org_id` — cross-tenant inference is forbidden by the spec.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from tender_agent.models import VaultDocument, VaultDocumentVersion


@dataclass(frozen=True)
class VaultFact:
    """A single, vault-grounded piece of evidence with provenance — every
    contradiction we emit later refers back to one of these so the dashboard
    can render `source_document` truthfully."""

    document_id: int
    version_id: int
    category: str
    title: str
    expiry_date: date | None
    claims: dict


def fetch_current_vault_facts(db: Session, *, org_id: int) -> list[VaultFact]:
    """All non-superseded vault documents for the tenant.

    A document with no current_version (mid-upload) is skipped — the
    reconciliation engine can only compare what has been extracted."""
    stmt = (
        select(VaultDocument, VaultDocumentVersion)
        .join(
            VaultDocumentVersion,
            VaultDocument.current_version_id == VaultDocumentVersion.id,
        )
        .where(VaultDocument.org_id == org_id)
        .where(VaultDocument.deleted_at.is_(None))
    )
    out: list[VaultFact] = []
    for doc, version in db.execute(stmt).all():
        out.append(
            VaultFact(
                document_id=doc.id,
                version_id=version.id,
                category=doc.category,
                title=version.title or doc.title,
                expiry_date=version.expiry_date,
                claims=version.claims or {},
            )
        )
    return out


# ---------------------------------------------------------------------------
# Projections — tiny helpers the reconciliation calls instead of poking the
# claims dict directly. Keeps the comparison loop readable.
# ---------------------------------------------------------------------------


def latest_turnover(facts: list[VaultFact]) -> tuple[VaultFact, Decimal] | None:
    """Most recent (by fiscal_year_end) accounts row that has a turnover."""
    candidates: list[tuple[date, VaultFact, Decimal]] = []
    for fact in facts:
        if fact.claims.get("doc_type") != "accounts":
            continue
        turnover = fact.claims.get("turnover")
        if turnover is None:
            continue
        try:
            value = Decimal(str(turnover))
        except Exception:  # noqa: BLE001
            continue
        fye = fact.claims.get("fiscal_year_end")
        sort_key = _safe_date(fye) or date.min
        candidates.append((sort_key, fact, value))
    if not candidates:
        return None
    candidates.sort(key=lambda triple: triple[0], reverse=True)
    _, fact, value = candidates[0]
    return fact, value


def insurance_certs(
    facts: list[VaultFact], *, insurance_type: str | None
) -> list[tuple[VaultFact, Decimal | None]]:
    """All vault insurance certs (optionally filtered to one type). Returns
    (fact, cover_amount) — cover_amount may be None for ambiguously-extracted
    certs and the caller treats that as 'no evidence either way'."""
    out: list[tuple[VaultFact, Decimal | None]] = []
    for fact in facts:
        if fact.claims.get("doc_type") != "insurance_certificate":
            continue
        if insurance_type and fact.claims.get("insurance_type") != insurance_type:
            continue
        raw = fact.claims.get("cover_amount")
        if raw is None:
            out.append((fact, None))
            continue
        try:
            out.append((fact, Decimal(str(raw))))
        except Exception:  # noqa: BLE001
            out.append((fact, None))
    return out


def iso_certs(
    facts: list[VaultFact], *, standard: str | None
) -> list[VaultFact]:
    """Vault ISO certs whose `standard` claim contains the requested number.
    Standard comparison is substring on the digit portion, so "27001" and
    "27001:2022" both match a "27001" probe."""
    target = (standard or "").strip()
    out: list[VaultFact] = []
    for fact in facts:
        if fact.claims.get("doc_type") != "iso_certificate":
            continue
        claim_std = (fact.claims.get("standard") or "").strip()
        if target and target not in claim_std:
            continue
        out.append(fact)
    return out


def accreditation_present(
    facts: list[VaultFact], *, standard: str
) -> list[VaultFact]:
    """Search the vault by title / claims for the named accreditation. We
    treat ISO docs whose standard matches AND any document whose title
    contains the canonical name as evidence."""
    needle = standard.lower().replace("_", " ")
    out: list[VaultFact] = []
    for fact in facts:
        if needle in (fact.title or "").lower():
            out.append(fact)
            continue
        # Cyber Essentials Plus is sometimes lodged as an ISO-shaped claim
        # with the standard string carrying the name; honour that.
        std = (fact.claims.get("standard") or "").lower()
        if needle in std:
            out.append(fact)
    return out


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------


def _safe_date(value: object) -> date | None:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None
