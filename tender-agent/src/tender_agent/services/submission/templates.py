"""Per-template response schemas + evidence_minimums + scoring_check + failure_modes.

Pinned to templates.yaml schema_version 1.6. Encoded as Python dataclasses
rather than loading the YAML at runtime, because:
  1. We need pydantic validators on the LLM output JSON anyway; this puts
     the schema + the validator in one place.
  2. The YAML is large (2362 lines) and not all of it is for this run — we
     stay focused on the five core text templates and ignore pricing /
     case-study / Gantt branches that are out of scope.

If the YAML moves to a newer schema_version, `SCHEMA_VERSION` here MUST be
bumped in lock-step. The drafting service refuses to draft if the requested
template_id isn't in `TEMPLATES`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

SCHEMA_VERSION = "1.6"

# The five core text templates this chunk supports. pricing_schedule and
# case_study are explicitly OUT of scope — see services.submission.__init__.
CORE_TEMPLATE_IDS = (
    "technical_capability",
    "methodology_delivery",
    "social_value",
    "quality_management",
    "risk_contingency",
)


@dataclass(frozen=True)
class FailIfRule:
    """A single scoring_check rule. fail_if is a textual condition the
    drafting agent renders into JSON output as a self-check result; we
    evaluate the named pattern checks deterministically post-LLM."""

    rule_id: str
    severity: str  # 'blocking' | 'non_blocking'
    description: str


@dataclass(frozen=True)
class TemplateSpec:
    template_id: str
    description: str
    # Required slots — the agent must populate each or list an unfilled_slot.
    required_slots: tuple[str, ...]
    # Required nested list slots — checked for min_items so the LLM can't
    # silently return an empty list.
    list_min_items: dict[str, int] = field(default_factory=dict)
    # evidence_minimums — name -> required count of occurrences across the
    # whole structured_content. The validator counts them.
    evidence_minimums: dict[str, int] = field(default_factory=dict)
    # Failure-mode regex patterns we check on the rendered text body
    # (joined strings inside structured_content). Matching == near-miss.
    failure_patterns: tuple[tuple[str, str], ...] = ()
    scoring_check: tuple[FailIfRule, ...] = ()


# ---------------------------------------------------------------------------
# Template definitions (pinned to templates.yaml 1.6)
# ---------------------------------------------------------------------------

TECHNICAL_CAPABILITY = TemplateSpec(
    template_id="technical_capability",
    description=(
        "How the tenant technically delivers the service. Typically highest "
        "single-question weighting (20–40%)."
    ),
    required_slots=(
        "headline",
        "methodology",
        "buyer_fit",
        "evidence",
        "team",
        "risks",
        "added_value",
        "kpis",
    ),
    list_min_items={
        "methodology.phases": 3,
        "methodology.standards": 1,
        "methodology.quality_gates": 2,
        "evidence.case_studies": 1,
        "team.named_people": 2,
        "risks.risks": 2,
        "added_value.items": 1,
        "kpis.kpis": 3,
    },
    evidence_minimums={
        "named_clients": 1,
        "certificate_numbers": 1,
        "named_people": 2,
        "numeric_kpis": 3,
        "buyer_doc_references": 1,
        "quantified_added_value": 1,
    },
    failure_patterns=(
        ("best_practice_unnamed", r"\bbest\s*practice\b"),
        ("industry_standard_unnamed", r"\bindustry\s*standard\b"),
        ("experienced_team", r"\bexperienced\s+team\b"),
        ("qualified_engineers", r"\bqualified\s+engineers\b"),
    ),
    scoring_check=(
        FailIfRule(
            "methodology_named",
            "blocking",
            "Response uses 'best practice' or 'industry standard' without "
            "naming a specific methodology.",
        ),
        FailIfRule(
            "named_people",
            "blocking",
            "Response mentions 'experienced team' / 'qualified engineers' "
            "with no named individuals.",
        ),
        FailIfRule(
            "contract_specific",
            "non_blocking",
            "Risks look generic — should be specific to this contract.",
        ),
    ),
)

METHODOLOGY_DELIVERY = TemplateSpec(
    template_id="methodology_delivery",
    description=(
        "Delivery plan / methodology — phase-by-phase how you'll execute, "
        "drawn from the bid's Schedule object."
    ),
    required_slots=(
        "headline",
        "mobilisation",
        "phases",
        "governance",
        "fte_plan",
        "kpis",
    ),
    list_min_items={
        "mobilisation.week_by_week": 4,
        "phases": 3,
        "governance.checkpoints": 2,
        "fte_plan.phases": 3,
        "kpis": 3,
    },
    evidence_minimums={
        "phases": 3,
        "named_people": 1,
        "kpis": 3,
    },
    failure_patterns=(
        ("team_unnamed_size", r"\bteam\s+of\s+experts?\b"),
        ("methodology_unnamed", r"\bbest\s*practice\b"),
    ),
    scoring_check=(
        FailIfRule(
            "mobilisation_week_by_week",
            "blocking",
            "Mobilisation must be week-by-week for weeks 1–4 minimum.",
        ),
        FailIfRule(
            "named_fte_per_phase",
            "non_blocking",
            "FTE per phase, not just 'team'.",
        ),
    ),
)

SOCIAL_VALUE = TemplateSpec(
    template_id="social_value",
    description=(
        "Social value commitments — SMART, evidenced, with vault-backed "
        "capability for every commitment."
    ),
    required_slots=(
        "selected_policy_outcome",
        "sv_lead",
        "commitments",
        "past_delivery",
        "measurement",
    ),
    list_min_items={
        "commitments": 3,
        "past_delivery": 1,
        "measurement.kpis": 2,
    },
    evidence_minimums={
        "commitments_with_capability_id": 3,
        "past_delivery_examples": 1,
        "named_sv_lead": 1,
    },
    failure_patterns=(
        ("support_communities_generic", r"\bsupport\s+local\s+communit"),
        ("commit_to_be_a_good_employer", r"\bgood\s+employer\b"),
    ),
    scoring_check=(
        FailIfRule(
            "selected_policy_outcome_matches_buyer",
            "non_blocking",
            "Selected Policy Outcome should match the buyer's signposted "
            "outcome verbatim.",
        ),
        FailIfRule(
            "deliverability_hard_block",
            "blocking",
            "Every commitment must carry a vault_capability_id resolving "
            "to prior delivery evidence. Commitments without capability "
            "evidence are forbidden.",
        ),
    ),
)

QUALITY_MANAGEMENT = TemplateSpec(
    template_id="quality_management",
    description=(
        "Quality management — named QMS, certificate numbers with expiry, "
        "phase-by-phase quality activities, CI methodology with worked example."
    ),
    required_slots=(
        "headline",
        "qms_framework",
        "certifications",
        "phase_quality_activities",
        "ci_methodology",
        "action_thresholds",
    ),
    list_min_items={
        "certifications": 1,
        "phase_quality_activities": 3,
    },
    evidence_minimums={
        "certificate_numbers": 1,
        "certificate_expiry_dates": 1,
        "ci_worked_examples": 1,
    },
    failure_patterns=(
        ("iso_certified_unnumbered", r"\bISO\s*certified\b(?!\s*\d)"),
        ("commitment_to_quality", r"\bcommit(?:ment|ted)?\s+to\s+quality\b"),
    ),
    scoring_check=(
        FailIfRule(
            "cert_expiry_validity",
            "blocking",
            "Cited certificates must be valid through the contract delivery "
            "period; expired or <60-day-to-expiry certs are forbidden.",
        ),
        FailIfRule(
            "named_qms_framework",
            "blocking",
            "Quality framework must be named (ISO 9001 / Six Sigma / "
            "company-specific QMS name).",
        ),
    ),
)

RISK_CONTINGENCY = TemplateSpec(
    template_id="risk_contingency",
    description=(
        "Risk and contingency — contract-specific risks with inherent / "
        "residual / target_residual scores, named owners, risk reserve."
    ),
    required_slots=(
        "risks",
        "risk_reserve_pct",
        "joint_review_offer",
    ),
    list_min_items={
        "risks": 5,
    },
    evidence_minimums={
        "risks": 5,
        "risks_with_scores": 5,
        "risks_with_owners": 5,
    },
    failure_patterns=(
        ("staff_turnover_generic", r"\bstaff\s+turnover\b"),
        ("supply_chain_disruption_generic", r"\bsupply\s+chain\s+disruption\b"),
    ),
    scoring_check=(
        FailIfRule(
            "every_risk_scored",
            "blocking",
            "Every risk must carry inherent, residual, and target_residual "
            "scores.",
        ),
        FailIfRule(
            "named_owners_seniority",
            "non_blocking",
            "Risk owners should be named individuals at appropriate seniority.",
        ),
    ),
)


TEMPLATES: dict[str, TemplateSpec] = {
    t.template_id: t
    for t in (
        TECHNICAL_CAPABILITY,
        METHODOLOGY_DELIVERY,
        SOCIAL_VALUE,
        QUALITY_MANAGEMENT,
        RISK_CONTINGENCY,
    )
}


def get_template(template_id: str) -> TemplateSpec:
    """Lookup with a clean error when the requested template isn't in this
    chunk's scope. pricing_schedule / case_study are intentionally absent —
    raising here keeps OUT-of-scope templates out of the drafting path."""
    try:
        return TEMPLATES[template_id]
    except KeyError as exc:
        raise ValueError(
            f"template_id {template_id!r} is not in the supported set "
            f"{CORE_TEMPLATE_IDS!r} (pricing_schedule / case_study are "
            "deferred to later chunks)."
        ) from exc


def all_required_slots(template: TemplateSpec) -> tuple[str, ...]:
    return template.required_slots


def expected_response_shape(template: TemplateSpec) -> dict[str, Any]:
    """A documentation-grade dict the LLM prompt embeds so the model knows
    what JSON shape to return. Not a substitute for the runtime validator —
    it's purely advisory text inside the prompt."""
    return {
        "template_id": template.template_id,
        "schema_version": SCHEMA_VERSION,
        "structured_content": dict.fromkeys(template.required_slots, "..."),
        "vault_citations": ["<VaultDocumentVersion id>"],
        "confidence_scores": dict.fromkeys(template.required_slots, "<0-100>"),
        "unfilled_slots": [
            {"slot_path": "<slot.subslot>", "reason": "<why null>"}
        ],
        "cross_section_alignments": [],
        "feedback_consumed": {
            "bid_pricing_history_ids": [],
            "bid_feedback_record_ids": [],
            "lessons_applied": [],
        },
    }
