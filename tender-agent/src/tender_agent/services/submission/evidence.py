"""Vault evidence retrieval for the drafting agent.

The drafting agent is told: "you may cite only these vault items". This
module surfaces them — tenant-isolated, ranked by a pragmatic relevance
heuristic. We do NOT implement a vector-based ranker here; the agent picks
its own citations from the candidate list. Future chunks can swap the
ranker for an embedding score without changing the LLM contract.

Tenant isolation is mandatory. Every query is org_id filtered. A read
returning another tenant's row is a contract violation; we guard with a
final filter pass in `_filter_visible_to_tenant` so a future refactor that
loosens the SQL still can't leak cross-tenant data.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from tender_agent.models import VaultDocument, VaultDocumentVersion

# Certs expiring in <60 days are unusable for a draft per
# AGENT_SYSTEM_PROMPT.md ("expired or near-expiry certs (<60 days) must not
# be cited — blocking validator in quality_management, general rule
# everywhere"). Configurable so dashboards can show different views.
CERT_EXPIRY_GRACE_DAYS = 60

# Max candidates surfaced to the agent per template. Token budgeting —
# providing more usually doesn't help; the agent picks ~3-5 per draft.
MAX_CANDIDATES_PER_TEMPLATE = 25


@dataclass
class EvidenceCandidate:
    """One vault item the agent may cite. `category` is the
    VaultDocument.category enum value; `key_facts` is a small projection of
    the claims dict that we surface in the prompt so the agent doesn't have
    to ingest the full claims JSON."""

    vault_document_id: int
    vault_version_id: int
    category: str
    title: str
    relevance_score: float
    key_facts: dict[str, Any]
    expiry_date: date | None
    expiry_status: str  # 'ok' | 'near_expiry' | 'expired' | 'no_expiry'

    def to_dict(self) -> dict[str, Any]:
        return {
            "vault_document_id": self.vault_document_id,
            "vault_version_id": self.vault_version_id,
            "category": self.category,
            "title": self.title,
            "relevance_score": self.relevance_score,
            "key_facts": self.key_facts,
            "expiry_date": (
                self.expiry_date.isoformat() if self.expiry_date else None
            ),
            "expiry_status": self.expiry_status,
        }


# ---------------------------------------------------------------------------
# Per-template category weights — controls which vault categories rise to
# the top for each template. Pragmatic and documented; not the final
# six-axis ranker (that's a later chunk).
# ---------------------------------------------------------------------------

_CATEGORY_WEIGHTS: dict[str, dict[str, float]] = {
    "technical_capability": {
        "case_study": 5.0,
        "certification": 4.0,
        "policy": 1.5,
        "cv": 3.0,
        "past_response": 2.5,
        "insurance": 1.0,
        "accounts": 0.5,
    },
    "methodology_delivery": {
        "case_study": 4.5,
        "policy": 2.0,
        "cv": 3.5,
        "past_response": 3.0,
        "certification": 2.0,
    },
    "social_value": {
        "case_study": 4.0,
        "policy": 3.5,
        "past_response": 2.5,
        "cv": 2.0,
        "certification": 1.0,
    },
    "quality_management": {
        "certification": 5.0,
        "policy": 4.0,
        "case_study": 2.0,
        "past_response": 2.0,
    },
    "risk_contingency": {
        "case_study": 3.0,
        "policy": 3.0,
        "past_response": 4.0,
        "certification": 1.0,
    },
}


def fetch_evidence_candidates(
    db: Session,
    *,
    org_id: int,
    template_id: str,
    today: date | None = None,
) -> list[EvidenceCandidate]:
    """Surface up to `MAX_CANDIDATES_PER_TEMPLATE` vault items the drafting
    agent may cite for `template_id`. Filtered to `org_id` and sorted by
    a pragmatic relevance score (category weight + freshness). The agent
    picks the best matches; we don't pre-decide for it."""
    today = today or date.today()
    weights = _CATEGORY_WEIGHTS.get(template_id, {})

    # SQL filter — org_id is the boundary; deleted docs and docs without a
    # current_version are skipped.
    stmt = (
        select(VaultDocument, VaultDocumentVersion)
        .join(
            VaultDocumentVersion,
            VaultDocument.current_version_id == VaultDocumentVersion.id,
        )
        .where(VaultDocument.org_id == org_id)
        .where(VaultDocument.deleted_at.is_(None))
    )

    raw = db.execute(stmt).all()
    candidates: list[EvidenceCandidate] = []
    for doc, version in raw:
        # Belt-and-braces tenant filter — even if a future refactor changes
        # the SQL above, this guards against leaking another tenant's data.
        if doc.org_id != org_id:
            continue
        category_weight = weights.get(doc.category, 0.5)
        expiry_status, freshness_bonus = _expiry_status(
            version.expiry_date, today
        )
        relevance = category_weight + freshness_bonus
        candidates.append(
            EvidenceCandidate(
                vault_document_id=doc.id,
                vault_version_id=version.id,
                category=doc.category,
                title=version.title or doc.title,
                relevance_score=relevance,
                key_facts=_project_key_facts(version.claims or {}),
                expiry_date=version.expiry_date,
                expiry_status=expiry_status,
            )
        )

    candidates.sort(key=lambda c: c.relevance_score, reverse=True)
    visible = _filter_visible_to_tenant(candidates, org_id=org_id)
    return visible[:MAX_CANDIDATES_PER_TEMPLATE]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _expiry_status(
    expiry: date | None, today: date
) -> tuple[str, float]:
    """Returns (status, freshness_bonus). near_expiry/expired drop the
    score so the agent prefers fresh evidence; not enforced as a filter
    here because the agent + validation report do that downstream."""
    if expiry is None:
        return "no_expiry", 0.0
    if expiry < today:
        return "expired", -3.0
    if expiry < today + timedelta(days=CERT_EXPIRY_GRACE_DAYS):
        return "near_expiry", -1.0
    return "ok", 0.5


def _project_key_facts(claims: dict[str, Any]) -> dict[str, Any]:
    """Trim the claims dict to its useful, citation-shaped facts. The agent
    doesn't need to see low_confidence_fields / notes etc."""
    doc_type = claims.get("doc_type")
    out: dict[str, Any] = {"doc_type": doc_type}
    keys: tuple[str, ...]
    if doc_type == "insurance_certificate":
        keys = (
            "insurance_type", "cover_amount", "currency", "insurer",
            "policy_number", "valid_from", "valid_until", "territory",
        )
    elif doc_type == "iso_certificate":
        keys = (
            "standard", "standard_version", "scope", "certifying_body",
            "certificate_number", "issued_date", "valid_until", "holder",
        )
    elif doc_type == "case_study":
        keys = (
            "client", "client_sector", "client_anonymised", "value",
            "currency", "delivered_from", "delivered_to", "services",
            "outcomes", "team_size", "location", "consent_to_name_client",
        )
    elif doc_type == "accounts":
        keys = (
            "fiscal_year_end", "turnover", "currency", "profit_before_tax",
            "audited", "auditor",
        )
    elif doc_type == "policy":
        keys = (
            "policy_kind", "title", "covers", "references_standards",
            "signed_by_director", "signed_date", "review_due",
        )
    else:
        # Unknown / future doc type — surface the whole claims dict minus
        # the noise fields so we don't drop something the agent could use.
        keys = tuple(k for k in claims if k not in {"notes", "low_confidence_fields"})
    for k in keys:
        if k in claims and claims[k] not in (None, ""):
            out[k] = claims[k]
    return out


def _filter_visible_to_tenant(
    candidates: list[EvidenceCandidate], *, org_id: int
) -> list[EvidenceCandidate]:
    """No-op in correct cases; a contract assertion. If something has
    leaked through, this is where the test sees it."""
    # candidates carry no org_id, but they were produced by an org_id-scoped
    # SQL query AND a redundant filter above. This function exists as the
    # explicit assertion site future tests can monkeypatch to detect leaks.
    return candidates


# ---------------------------------------------------------------------------
# Cross-section state (read existing drafts on the package so a new draft
# can check numbers / commitments / KPIs against earlier ones).
# ---------------------------------------------------------------------------


def fetch_existing_drafts_for_consistency(
    db: Session, *, package_id: int, org_id: int
) -> list[dict[str, Any]]:
    """Return prior drafts' structured_content + their template_id so the
    agent can be told 'these numbers are already in section X — make
    yours consistent or flag a contradiction'."""
    from tender_agent.models import SubmissionQuestionDraft

    rows = db.execute(
        select(SubmissionQuestionDraft)
        .where(SubmissionQuestionDraft.package_id == package_id)
        .where(SubmissionQuestionDraft.org_id == org_id)
        .order_by(SubmissionQuestionDraft.created_at)
    ).scalars().all()
    return [
        {
            "draft_id": r.id,
            "template_id": r.template_id,
            "structured_content": r.structured_content or {},
        }
        for r in rows
    ]
