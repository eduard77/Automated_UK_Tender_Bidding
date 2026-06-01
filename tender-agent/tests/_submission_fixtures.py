"""Shared fixtures + a fake LLM client for the submission tests.

ZERO real network. Every test uses one of the fakes below — the production
AnthropicDraftingLLMClient is never imported here.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from tender_agent.models import (
    Tender,
    TenderBrief,
    VaultDocument,
    VaultDocumentVersion,
)
from tender_agent.services.submission.llm_client import LLMResponse
from tests._billing_fixtures import make_engine_and_session  # SQLite + type shims

# ---------------------------------------------------------------------------
# Fake LLM clients
# ---------------------------------------------------------------------------


@dataclass
class FakeDraftingLLMClient:
    """In-memory fake. Pass a response_factory: it receives (system, user)
    and returns the text the LLM would have produced. Tests use this to
    feed schema-valid JSON, malformed JSON, etc."""

    response_factory: Callable[[str, str], str]
    model: str = "fake-model"
    calls: list[tuple[str, str]] = field(default_factory=list)

    async def complete(
        self, *, system: str, user: str, max_tokens: int
    ) -> LLMResponse:
        self.calls.append((system, user))
        text = self.response_factory(system, user)
        return LLMResponse(
            text=text, input_tokens=100, output_tokens=200, model=self.model
        )


def static_response(payload: dict[str, Any]) -> Callable[[str, str], str]:
    """Returns a factory that always returns the JSON-serialised payload."""
    body = json.dumps(payload)
    return lambda _system, _user: body


def sequential_responses(
    *payloads: dict[str, Any] | str,
) -> Callable[[str, str], str]:
    """Cycle through multiple responses — first call returns payloads[0],
    next call payloads[1], etc. Used to test retry behaviour."""
    counter = {"i": 0}

    def _factory(_system: str, _user: str) -> str:
        i = counter["i"]
        counter["i"] = min(i + 1, len(payloads) - 1)
        p = payloads[i]
        return p if isinstance(p, str) else json.dumps(p)

    return _factory


# ---------------------------------------------------------------------------
# DB builders (SQLite via _billing_fixtures)
# ---------------------------------------------------------------------------


_T_COUNTER = {"n": 0}


def fresh_engine():
    return make_engine_and_session()


def make_tender(
    db: Session,
    *,
    title: str = "Cyber security retrofit",
    value_amount: Decimal | None = Decimal(200_000),
    contract_end: date | None = None,
) -> Tender:
    _T_COUNTER["n"] += 1
    t = Tender(
        source_code="TEST",
        source_ref=f"sub-{_T_COUNTER['n']}",
        title=title,
        value_amount=value_amount,
        contract_end=contract_end,
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def make_complete_brief(db: Session, *, tender_id: int) -> TenderBrief:
    b = TenderBrief(
        tender_id=tender_id,
        status="complete",
        recommendation="bid",
        confidence="high",
        headline="Bid",
        brief_json={
            "recommendation": "bid",
            "confidence": "high",
            "headline": "Bid: clean cyber-security retrofit",
            "scope_summary": "Replace legacy SOC tooling; 24/7 cover.",
            "mandatory_requirements": [
                "ISO 27001 certified",
                "£10m Professional Indemnity insurance",
            ],
            "key_risks": [{"title": "Tight deadline", "detail": "14 days."}],
        },
        generated_at=datetime.now(UTC),
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    db.add(b)
    db.commit()
    db.refresh(b)
    return b


def make_vault_doc(
    db: Session,
    *,
    org_id: int = 1,
    category: str,
    title: str,
    claims: dict[str, Any],
    expiry: date | None = None,
) -> int:
    doc = VaultDocument(org_id=org_id, category=category, title=title)
    db.add(doc)
    db.flush()
    v = VaultDocumentVersion(
        document_id=doc.id,
        version=1,
        storage_key="x",
        bytes=1,
        sha256=f"sha-{title}-{org_id}",
        mime_type="application/pdf",
        title=title,
        expiry_date=expiry,
        claims=claims,
    )
    db.add(v)
    db.flush()
    doc.current_version_id = v.id
    db.commit()
    return v.id


def populate_typical_vault(db: Session, *, org_id: int = 1) -> dict[str, int]:
    """A small representative vault — used by most drafting tests so each
    template has plausible candidates to cite."""
    return {
        "iso_27001": make_vault_doc(
            db,
            org_id=org_id,
            category="certification",
            title="ISO 27001:2022 certificate",
            claims={
                "doc_type": "iso_certificate",
                "standard": "27001:2022",
                "certificate_number": "ISO-27001-12345",
                "valid_until": "2027-12-31",
                "holder": "Test Co Ltd",
            },
            expiry=date.today() + timedelta(days=365),
        ),
        "pi_insurance": make_vault_doc(
            db,
            org_id=org_id,
            category="insurance",
            title="PI Insurance 2025",
            claims={
                "doc_type": "insurance_certificate",
                "insurance_type": "professional_indemnity",
                "cover_amount": "10000000",
                "currency": "GBP",
                "insurer": "Acme Insurance Ltd",
                "policy_number": "PI-2025-001",
            },
            expiry=date.today() + timedelta(days=400),
        ),
        "case_study": make_vault_doc(
            db,
            org_id=org_id,
            category="case_study",
            title="Whitstable SOC retrofit",
            claims={
                "doc_type": "case_study",
                "client": "Whitstable Council",
                "client_sector": "local_government",
                "value": "2100000",
                "currency": "GBP",
                "outcomes": ["12% recycling lift", "99.7% SLA"],
            },
        ),
        "policy_quality": make_vault_doc(
            db,
            org_id=org_id,
            category="policy",
            title="Quality Management Policy",
            claims={
                "doc_type": "policy",
                "policy_kind": "quality",
                "title": "Test Co QMS Policy",
                "signed_by_director": True,
                "signed_date": "2025-01-15",
                "references_standards": ["ISO 9001:2015"],
            },
        ),
    }


# ---------------------------------------------------------------------------
# Reference draft payloads — schema-valid JSON for each template
# ---------------------------------------------------------------------------


def technical_capability_payload(
    *,
    vault_iso: int,
    vault_case_study: int,
    kpi_sla: str = "99.7%",
) -> dict[str, Any]:
    """A draft that passes every blocking validator for technical_capability."""
    return {
        "template_id": "technical_capability",
        "schema_version": "1.6",
        "structured_content": {
            "headline": (
                "We retrofit your SOC using PRINCE2 with ISO 27001 controls "
                "and a 99.7% SLA target."
            ),
            "methodology": {
                "methodology_name": "PRINCE2 with ISO 27001 controls",
                "phases": [
                    {
                        "name": "Discovery",
                        "deliverable": "Gap analysis",
                        "duration": "2 weeks",
                    },
                    {
                        "name": "Design",
                        "deliverable": "Target arch",
                        "duration": "3 weeks",
                    },
                    {
                        "name": "Cut-over",
                        "deliverable": "Operational SOC",
                        "duration": "4 weeks",
                    },
                ],
                "standards": [
                    {
                        "name": "ISO 27001:2022",
                        "certificate_number": "ISO-27001-12345",
                        "expiry_date": "2027-12-31",
                    }
                ],
                "quality_gates": [
                    "Stage Gate A: Discovery sign-off",
                    "Stage Gate B: Design sign-off",
                ],
            },
            "buyer_fit": {
                "strategy_reference": (
                    "Aligned to your Digital Strategy 2025 priority 3."
                ),
                "specific_adaptation": (
                    "Region-aware deployment for North-West sites."
                ),
            },
            "evidence": {
                "case_studies": [
                    {
                        "client_or_label": "Whitstable Council",
                        "value_gbp": 2_100_000,
                        "year": 2024,
                        "outcome_metric": "99.7% SLA achievement",
                        "vault_id": vault_case_study,
                    }
                ]
            },
            "team": {
                "named_people": [
                    {
                        "name": "Alex Rowe",
                        "role": "Operations Director",
                        "qualification": "PRINCE2 Practitioner",
                        "years_experience": 12,
                        "similar_project": "Whitstable SOC",
                    },
                    {
                        "name": "Bina Patel",
                        "role": "Lead Architect",
                        "qualification": "CISSP",
                        "years_experience": 9,
                        "similar_project": "Acme Retail SOC",
                    },
                ]
            },
            "risks": {
                "risks": [
                    {
                        "risk": "Late ITSM tooling decision blocks cut-over",
                        "mitigation": "Parallel evaluation in week 1",
                        "owner": "Alex Rowe",
                    },
                    {
                        "risk": "TUPE transfer of 11 analysts",
                        "mitigation": "HR-led 90-day plan",
                        "owner": "Bina Patel",
                    },
                ]
            },
            "added_value": {
                "items": [
                    {
                        "description": "Quarterly threat-intel briefings",
                        "quantification": "4 briefings / yr",
                        "value_to_buyer": "Free of charge",
                    }
                ]
            },
            "kpis": {
                "kpis": [
                    {
                        "metric": "SLA achievement",
                        "target": kpi_sla,
                        "measurement_method": "Quarterly audit",
                    },
                    {
                        "metric": "MTTR",
                        "target": "2 hours",
                        "measurement_method": "Ticket telemetry",
                    },
                    {
                        "metric": "Phishing click-through",
                        "target": "<3%",
                        "measurement_method": "Simulation campaigns",
                    },
                ]
            },
        },
        "vault_citations": [vault_iso, vault_case_study],
        "confidence_scores": {
            "methodology": 95,
            "buyer_fit": 80,
            "evidence": 95,
            "team": 95,
            "risks": 85,
            "added_value": 70,
            "kpis": 90,
        },
        "unfilled_slots": [],
        "cross_section_alignments": [],
        "feedback_consumed": {
            "bid_pricing_history_ids": [],
            "bid_feedback_record_ids": [],
            "lessons_applied": [],
        },
    }


def methodology_delivery_payload(*, vault_case_study: int) -> dict[str, Any]:
    return {
        "template_id": "methodology_delivery",
        "schema_version": "1.6",
        "structured_content": {
            "headline": "Week-by-week mobilisation; PRINCE2 governance.",
            "mobilisation": {
                "week_by_week": [
                    {"week": 1, "activity": "Discovery kickoff"},
                    {"week": 2, "activity": "Gap analysis"},
                    {"week": 3, "activity": "Design review"},
                    {"week": 4, "activity": "Cut-over rehearsal"},
                ]
            },
            "phases": [
                {"name": "Discovery", "duration_weeks": 2},
                {"name": "Design", "duration_weeks": 3},
                {"name": "Cut-over", "duration_weeks": 4},
            ],
            "governance": {
                "checkpoints": [
                    "Weekly buyer steerco",
                    "Monthly risk review",
                ]
            },
            "fte_plan": {
                "phases": [
                    {"phase": "Discovery", "fte_count": 4},
                    {"phase": "Design", "fte_count": 6},
                    {"phase": "Cut-over", "fte_count": 8},
                ]
            },
            "kpis": [
                {"metric": "Velocity", "target": "10 story points"},
                {"metric": "On-time delivery", "target": "100%"},
                {"metric": "Defect leakage", "target": "<2%"},
            ],
        },
        "vault_citations": [vault_case_study],
        "confidence_scores": {"mobilisation": 90, "phases": 90},
        "unfilled_slots": [],
        "cross_section_alignments": [],
        "feedback_consumed": {
            "bid_pricing_history_ids": [],
            "bid_feedback_record_ids": [],
            "lessons_applied": [],
        },
    }


def social_value_payload(
    *, vault_case_study: int, kpi_apprenticeships: str = "8"
) -> dict[str, Any]:
    return {
        "template_id": "social_value",
        "schema_version": "1.6",
        "structured_content": {
            "selected_policy_outcome": "MAC 1.1 — Create new businesses, jobs and skills",
            "sv_lead": {
                "name": "Carol Quigley",
                "role": "Social Value Lead",
                "reports_to": "MD",
            },
            "commitments": [
                {
                    "id": "SV1",
                    "commit": (
                        "8 apprenticeships started in Year 1 in the buyer's "
                        "local authority area"
                    ),
                    "smart": True,
                    "vault_capability_id": vault_case_study,
                },
                {
                    "id": "SV2",
                    "commit": "200 hours volunteering by Year 1 end",
                    "smart": True,
                    "vault_capability_id": vault_case_study,
                },
                {
                    "id": "SV3",
                    "commit": "5 SME suppliers onboarded in Year 1",
                    "smart": True,
                    "vault_capability_id": vault_case_study,
                },
            ],
            "past_delivery": [
                {
                    "example": "Whitstable: 6 apprenticeships in 2024",
                    "vault_id": vault_case_study,
                }
            ],
            "measurement": {
                "kpis": [
                    {"metric": "Apprenticeships started", "target": kpi_apprenticeships},
                    {"metric": "Volunteering hours", "target": "200"},
                ]
            },
        },
        "vault_citations": [vault_case_study],
        "confidence_scores": {"commitments": 90, "past_delivery": 95},
        "unfilled_slots": [],
        "cross_section_alignments": [],
        "feedback_consumed": {
            "bid_pricing_history_ids": [],
            "bid_feedback_record_ids": [],
            "lessons_applied": [],
        },
    }


def quality_management_payload(*, vault_iso: int) -> dict[str, Any]:
    return {
        "template_id": "quality_management",
        "schema_version": "1.6",
        "structured_content": {
            "headline": "ISO 9001-based QMS with phase-gate quality reviews.",
            "qms_framework": {
                "name": "ISO 9001:2015 with Six Sigma toolkit",
            },
            "certifications": [
                {
                    "name": "ISO 9001:2015",
                    "certificate_number": "ISO-9001-9876",
                    "expiry_date": (date.today() + timedelta(days=400)).isoformat(),
                }
            ],
            "phase_quality_activities": [
                {"phase": "Discovery", "activity": "Quality kickoff"},
                {"phase": "Design", "activity": "Peer review"},
                {"phase": "Cut-over", "activity": "Final UAT"},
            ],
            "ci_methodology": {
                "framework": "PDCA",
                "worked_example": (
                    "Whitstable Q2 2024: defect rate dropped 30% after "
                    "PDCA cycle on triage."
                ),
            },
            "action_thresholds": {
                "satisfaction_below_target": "≤80% triggers root-cause review"
            },
        },
        "vault_citations": [vault_iso],
        "confidence_scores": {"certifications": 95, "qms_framework": 90},
        "unfilled_slots": [],
        "cross_section_alignments": [],
        "feedback_consumed": {
            "bid_pricing_history_ids": [],
            "bid_feedback_record_ids": [],
            "lessons_applied": [],
        },
    }


def risk_contingency_payload(*, vault_case_study: int) -> dict[str, Any]:
    return {
        "template_id": "risk_contingency",
        "schema_version": "1.6",
        "structured_content": {
            "risks": [
                {
                    "risk": "Late ITSM tooling decision",
                    "mitigation": "Parallel evaluation in week 1",
                    "owner": "Alex Rowe",
                    "inherent": 12,
                    "residual": 6,
                    "target_residual": 3,
                },
                {
                    "risk": "TUPE transfer of 11 analysts",
                    "mitigation": "HR-led 90-day plan",
                    "owner": "Bina Patel",
                    "inherent": 9,
                    "residual": 4,
                    "target_residual": 2,
                },
                {
                    "risk": "Buyer change-freeze in Q4",
                    "mitigation": "Pre-freeze deployment plan",
                    "owner": "Alex Rowe",
                    "inherent": 10,
                    "residual": 5,
                    "target_residual": 2,
                },
                {
                    "risk": "Subcontractor capacity for cut-over",
                    "mitigation": "Pre-booked T&M reserve",
                    "owner": "Bina Patel",
                    "inherent": 8,
                    "residual": 4,
                    "target_residual": 2,
                },
                {
                    "risk": "Cyber incident during transition",
                    "mitigation": "Hot-warm SOC overlap",
                    "owner": "Alex Rowe",
                    "inherent": 12,
                    "residual": 5,
                    "target_residual": 2,
                },
            ],
            "risk_reserve_pct": 4.0,
            "joint_review_offer": (
                "We offer a joint quarterly risk review with the buyer."
            ),
        },
        "vault_citations": [vault_case_study],
        "confidence_scores": {"risks": 85},
        "unfilled_slots": [],
        "cross_section_alignments": [],
        "feedback_consumed": {
            "bid_pricing_history_ids": [],
            "bid_feedback_record_ids": [],
            "lessons_applied": [],
        },
    }
