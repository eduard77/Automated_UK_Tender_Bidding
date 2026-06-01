"""Drafting engine tests — drafting + validation + cross-section + isolation
+ copyright + no-submit (mocked LLM, zero real network)."""
from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from tender_agent.models import (
    SubmissionPackage,
    SubmissionQuestionDraft,
)
from tender_agent.services.submission.engine import (
    BriefNotReadyForDrafting,
    DraftingAgent,
    DraftRequest,
)
from tender_agent.services.submission.evidence import fetch_evidence_candidates
from tender_agent.services.submission.templates import (
    CORE_TEMPLATE_IDS,
    SCHEMA_VERSION,
    TEMPLATES,
    get_template,
)
from tests._submission_fixtures import (
    FakeDraftingLLMClient,
    fresh_engine,
    make_complete_brief,
    make_tender,
    methodology_delivery_payload,
    populate_typical_vault,
    quality_management_payload,
    risk_contingency_payload,
    sequential_responses,
    social_value_payload,
    static_response,
    technical_capability_payload,
)


@pytest.fixture()
def db():
    _, factory = fresh_engine()
    s = factory()
    try:
        yield s
    finally:
        s.close()


def _make_request(template_id: str, *, question_text: str | None = None) -> DraftRequest:
    return DraftRequest(
        template_id=template_id,
        question_text=question_text or "Describe your technical approach.",
        question_weight_pct=25.0,
        word_limit=800,
        evaluation_criteria=(
            "Marked 0-5 on methodology specificity, evidence density, "
            "named team, contract-specific risks."
        ),
        buyer_strategy_docs=["Digital Strategy 2025"],
        buyer_operational_context="North-West sites, 24/7 cover required.",
        buyer_priorities=["Recycling rates", "Route optimisation"],
    )


# ---------------------------------------------------------------------------
# Templates registry
# ---------------------------------------------------------------------------


def test_only_five_core_templates_supported() -> None:
    assert set(CORE_TEMPLATE_IDS) == {
        "technical_capability",
        "methodology_delivery",
        "social_value",
        "quality_management",
        "risk_contingency",
    }
    # Out-of-scope templates are intentionally absent.
    with pytest.raises(ValueError):
        get_template("pricing_schedule")
    with pytest.raises(ValueError):
        get_template("case_study")


def test_schema_version_is_pinned() -> None:
    assert SCHEMA_VERSION == "1.6"


# ---------------------------------------------------------------------------
# Per-template happy path — each draft persists with status='needs_review'
# ---------------------------------------------------------------------------


async def test_drafts_technical_capability_from_fake_llm(db) -> None:
    tender = make_tender(db)
    make_complete_brief(db, tender_id=tender.id)
    vault = populate_typical_vault(db)
    payload = technical_capability_payload(
        vault_iso=vault["iso_27001"], vault_case_study=vault["case_study"]
    )
    client = FakeDraftingLLMClient(static_response(payload))
    agent = DraftingAgent(client)

    outcome = await agent.draft_question(
        db,
        tender_id=tender.id,
        org_id=1,
        request=_make_request("technical_capability"),
    )

    assert outcome.draft.status == "needs_review"
    assert outcome.draft.template_id == "technical_capability"
    assert outcome.draft.schema_version == "1.6"
    assert outcome.draft.structured_content["methodology"]["methodology_name"]
    assert outcome.draft.vault_citations
    assert isinstance(outcome.draft.validation_report, dict)


async def test_drafts_methodology_delivery(db) -> None:
    tender = make_tender(db)
    make_complete_brief(db, tender_id=tender.id)
    vault = populate_typical_vault(db)
    payload = methodology_delivery_payload(vault_case_study=vault["case_study"])
    agent = DraftingAgent(FakeDraftingLLMClient(static_response(payload)))
    outcome = await agent.draft_question(
        db,
        tender_id=tender.id,
        org_id=1,
        request=_make_request("methodology_delivery"),
    )
    assert outcome.draft.status == "needs_review"


async def test_drafts_social_value(db) -> None:
    tender = make_tender(db)
    make_complete_brief(db, tender_id=tender.id)
    vault = populate_typical_vault(db)
    payload = social_value_payload(vault_case_study=vault["case_study"])
    agent = DraftingAgent(FakeDraftingLLMClient(static_response(payload)))
    outcome = await agent.draft_question(
        db, tender_id=tender.id, org_id=1, request=_make_request("social_value")
    )
    assert outcome.draft.status == "needs_review"


async def test_drafts_quality_management(db) -> None:
    tender = make_tender(db)
    make_complete_brief(db, tender_id=tender.id)
    vault = populate_typical_vault(db)
    payload = quality_management_payload(vault_iso=vault["iso_27001"])
    agent = DraftingAgent(FakeDraftingLLMClient(static_response(payload)))
    outcome = await agent.draft_question(
        db, tender_id=tender.id, org_id=1, request=_make_request("quality_management")
    )
    assert outcome.draft.status == "needs_review"


async def test_drafts_risk_contingency(db) -> None:
    tender = make_tender(db)
    make_complete_brief(db, tender_id=tender.id)
    vault = populate_typical_vault(db)
    payload = risk_contingency_payload(vault_case_study=vault["case_study"])
    agent = DraftingAgent(FakeDraftingLLMClient(static_response(payload)))
    outcome = await agent.draft_question(
        db, tender_id=tender.id, org_id=1, request=_make_request("risk_contingency")
    )
    assert outcome.draft.status == "needs_review"


# ---------------------------------------------------------------------------
# No invention — null + unfilled_slots, not fabricated content
# ---------------------------------------------------------------------------


async def test_unfilled_slot_is_preserved_no_fabrication(db) -> None:
    tender = make_tender(db)
    make_complete_brief(db, tender_id=tender.id)
    vault = populate_typical_vault(db)
    payload = technical_capability_payload(
        vault_iso=vault["iso_27001"], vault_case_study=vault["case_study"]
    )
    # Empty out a required slot AND record the gap as the agent would.
    payload["structured_content"]["added_value"] = None
    payload["unfilled_slots"].append(
        {
            "slot_path": "added_value",
            "reason": "no vault evidence of added-value commitments",
        }
    )

    agent = DraftingAgent(FakeDraftingLLMClient(static_response(payload)))
    outcome = await agent.draft_question(
        db, tender_id=tender.id, org_id=1, request=_make_request("technical_capability")
    )
    assert outcome.draft.structured_content.get("added_value") is None
    assert any(
        s.get("slot_path") == "added_value"
        for s in outcome.draft.unfilled_slots
    )
    # No TBD / placeholder text smuggled in.
    serialized = json.dumps(outcome.draft.structured_content)
    assert "TBD" not in serialized
    assert "to be confirmed" not in serialized.lower()


# ---------------------------------------------------------------------------
# Blocking validator failure → status='incomplete', surfaced
# ---------------------------------------------------------------------------


async def test_unnamed_methodology_blocks_to_incomplete(db) -> None:
    tender = make_tender(db)
    make_complete_brief(db, tender_id=tender.id)
    vault = populate_typical_vault(db)
    payload = technical_capability_payload(
        vault_iso=vault["iso_27001"], vault_case_study=vault["case_study"]
    )
    payload["structured_content"]["methodology"]["methodology_name"] = (
        "best practice"
    )
    agent = DraftingAgent(FakeDraftingLLMClient(static_response(payload)))
    outcome = await agent.draft_question(
        db, tender_id=tender.id, org_id=1, request=_make_request("technical_capability")
    )
    assert outcome.draft.status == "incomplete"
    failed = [
        c for c in outcome.validation_report.scoring_check_results
        if c.name == "methodology_named" and not c.pass_
    ]
    assert failed
    assert outcome.validation_report.is_blocking_failure is True


async def test_social_value_commit_without_capability_id_blocks(db) -> None:
    tender = make_tender(db)
    make_complete_brief(db, tender_id=tender.id)
    vault = populate_typical_vault(db)
    payload = social_value_payload(vault_case_study=vault["case_study"])
    # Strip vault_capability_id from one commitment — spec hard-blocks.
    payload["structured_content"]["commitments"][0].pop("vault_capability_id")
    agent = DraftingAgent(FakeDraftingLLMClient(static_response(payload)))
    outcome = await agent.draft_question(
        db, tender_id=tender.id, org_id=1, request=_make_request("social_value")
    )
    assert outcome.draft.status == "incomplete"


async def test_quality_mgmt_expired_cert_blocks(db) -> None:
    tender = make_tender(db)
    make_complete_brief(db, tender_id=tender.id)
    vault = populate_typical_vault(db)
    payload = quality_management_payload(vault_iso=vault["iso_27001"])
    # Backdate the cert expiry → blocking failure.
    payload["structured_content"]["certifications"][0]["expiry_date"] = (
        (date.today() - timedelta(days=1)).isoformat()
    )
    agent = DraftingAgent(FakeDraftingLLMClient(static_response(payload)))
    outcome = await agent.draft_question(
        db, tender_id=tender.id, org_id=1, request=_make_request("quality_management")
    )
    assert outcome.draft.status == "incomplete"


async def test_risk_without_scores_blocks(db) -> None:
    tender = make_tender(db)
    make_complete_brief(db, tender_id=tender.id)
    vault = populate_typical_vault(db)
    payload = risk_contingency_payload(vault_case_study=vault["case_study"])
    payload["structured_content"]["risks"][0].pop("inherent")
    agent = DraftingAgent(FakeDraftingLLMClient(static_response(payload)))
    outcome = await agent.draft_question(
        db, tender_id=tender.id, org_id=1, request=_make_request("risk_contingency")
    )
    assert outcome.draft.status == "incomplete"


# ---------------------------------------------------------------------------
# evidence_minimums populate the validation_report
# ---------------------------------------------------------------------------


async def test_evidence_minimums_populated_in_report(db) -> None:
    tender = make_tender(db)
    make_complete_brief(db, tender_id=tender.id)
    vault = populate_typical_vault(db)
    payload = technical_capability_payload(
        vault_iso=vault["iso_27001"], vault_case_study=vault["case_study"]
    )
    agent = DraftingAgent(FakeDraftingLLMClient(static_response(payload)))
    outcome = await agent.draft_question(
        db, tender_id=tender.id, org_id=1, request=_make_request("technical_capability")
    )
    em = outcome.validation_report.evidence_minimums
    template = get_template("technical_capability")
    # Every key from the spec's evidence_minimums is reported.
    for key in template.evidence_minimums:
        assert key in em
        assert "required" in em[key] and "found" in em[key] and "pass" in em[key]


# ---------------------------------------------------------------------------
# Cross-section consistency
# ---------------------------------------------------------------------------


async def test_kpi_mismatch_across_drafts_is_flagged(db) -> None:
    tender = make_tender(db)
    make_complete_brief(db, tender_id=tender.id)
    vault = populate_typical_vault(db)
    # First draft commits to "99.7%" SLA.
    payload_a = technical_capability_payload(
        vault_iso=vault["iso_27001"], vault_case_study=vault["case_study"]
    )
    agent = DraftingAgent(FakeDraftingLLMClient(static_response(payload_a)))
    out_a = await agent.draft_question(
        db, tender_id=tender.id, org_id=1, request=_make_request("technical_capability")
    )
    assert out_a.draft.status == "needs_review"

    # Second draft on the SAME package — same metric name, different value.
    payload_b = technical_capability_payload(
        vault_iso=vault["iso_27001"],
        vault_case_study=vault["case_study"],
        kpi_sla="95.0%",
    )
    agent_b = DraftingAgent(FakeDraftingLLMClient(static_response(payload_b)))
    out_b = await agent_b.draft_question(
        db, tender_id=tender.id, org_id=1, request=_make_request("technical_capability")
    )
    assert out_b.draft.status == "incomplete"
    cs = out_b.validation_report.cross_section_validation
    assert cs["consistent"] is False
    assert any(i["metric"] == "sla achievement" for i in cs["inconsistencies"])


# ---------------------------------------------------------------------------
# Tenant isolation — another org's vault item is never offered as a candidate
# ---------------------------------------------------------------------------


def test_evidence_candidates_filtered_by_org_id(db) -> None:
    populate_typical_vault(db, org_id=1)
    populate_typical_vault(db, org_id=999)
    candidates = fetch_evidence_candidates(
        db, org_id=1, template_id="technical_capability"
    )
    # None of the candidates should be tenant 999's rows. We assert by
    # checking how many candidates the other tenant has would be visible
    # if we lifted the filter — should be the same 4 we just inserted —
    # but the actual visible count for tenant 1 equals the tenant-1 vault.
    visible_count = len(candidates)
    assert visible_count > 0
    # Cross-check: requesting tenant 999 returns a separate set.
    other = fetch_evidence_candidates(
        db, org_id=999, template_id="technical_capability"
    )
    assert other and {c.vault_version_id for c in candidates}.isdisjoint(
        {c.vault_version_id for c in other}
    )


# ---------------------------------------------------------------------------
# Copyright — verbatim run >15 words from the ITT blocks
# ---------------------------------------------------------------------------


async def test_long_verbatim_itt_run_blocks(db) -> None:
    tender = make_tender(db)
    make_complete_brief(db, tender_id=tender.id)
    vault = populate_typical_vault(db)
    itt_quote = (
        "Greendale Council requires bidders to demonstrate a route-optimised "
        "waste collection service with measurable recycling improvements "
        "and a strong social-value commitment to the local community across "
        "all wards."
    )
    payload = technical_capability_payload(
        vault_iso=vault["iso_27001"], vault_case_study=vault["case_study"]
    )
    # Smuggle the verbatim ITT into the buyer_fit slot.
    payload["structured_content"]["buyer_fit"]["strategy_reference"] = itt_quote

    agent = DraftingAgent(FakeDraftingLLMClient(static_response(payload)))
    outcome = await agent.draft_question(
        db,
        tender_id=tender.id,
        org_id=1,
        request=_make_request(
            "technical_capability", question_text=itt_quote
        ),
    )
    assert outcome.draft.status == "incomplete"
    cc = outcome.validation_report.copyright_check
    assert cc["verbatim_run_too_long"] is True
    assert cc["longest_run_words"] > 15


async def test_short_verbatim_phrase_does_not_block(db) -> None:
    tender = make_tender(db)
    make_complete_brief(db, tender_id=tender.id)
    vault = populate_typical_vault(db)
    payload = technical_capability_payload(
        vault_iso=vault["iso_27001"], vault_case_study=vault["case_study"]
    )
    agent = DraftingAgent(FakeDraftingLLMClient(static_response(payload)))
    outcome = await agent.draft_question(
        db,
        tender_id=tender.id,
        org_id=1,
        request=_make_request(
            "technical_capability",
            question_text=(
                "Describe your route-optimised approach to waste collection."
            ),
        ),
    )
    assert outcome.draft.status == "needs_review"
    assert outcome.validation_report.copyright_check["verbatim_run_too_long"] is False


# ---------------------------------------------------------------------------
# No-submit invariant
# ---------------------------------------------------------------------------


def test_default_draft_status_is_needs_review_never_submitted() -> None:
    """Contract guarantee: the model's default status is a review state,
    and the column docstring documents the absence of any submitted state.

    The engine's drafting tests (above) cover the dynamic side — under no
    LLM input can the engine produce a draft with status='submitted'."""
    from tender_agent.models import SubmissionQuestionDraft as Model

    # Default status comes from the column definition.
    default = (
        Model.__table__.c.status.default.arg
        if Model.__table__.c.status.default is not None
        else None
    )
    assert default == "needs_review"


async def test_draft_status_only_needs_review_or_incomplete(db) -> None:
    tender = make_tender(db)
    make_complete_brief(db, tender_id=tender.id)
    vault = populate_typical_vault(db)
    payload = technical_capability_payload(
        vault_iso=vault["iso_27001"], vault_case_study=vault["case_study"]
    )
    agent = DraftingAgent(FakeDraftingLLMClient(static_response(payload)))
    outcome = await agent.draft_question(
        db, tender_id=tender.id, org_id=1, request=_make_request("technical_capability")
    )
    assert outcome.draft.status in {"needs_review", "incomplete"}


# ---------------------------------------------------------------------------
# brief_not_ready / unknown tender
# ---------------------------------------------------------------------------


async def test_brief_not_ready_raises(db) -> None:
    tender = make_tender(db)
    # No brief generated.
    agent = DraftingAgent(FakeDraftingLLMClient(static_response({})))
    with pytest.raises(BriefNotReadyForDrafting):
        await agent.draft_question(
            db, tender_id=tender.id, org_id=1, request=_make_request("technical_capability")
        )


async def test_unknown_tender_raises(db) -> None:
    agent = DraftingAgent(FakeDraftingLLMClient(static_response({})))
    with pytest.raises(ValueError):
        await agent.draft_question(
            db, tender_id=999_999, org_id=1, request=_make_request("technical_capability")
        )


# ---------------------------------------------------------------------------
# Malformed LLM output → retry → clean failure, nothing stored broken
# ---------------------------------------------------------------------------


async def test_malformed_llm_json_retries_then_stores_incomplete(db) -> None:
    tender = make_tender(db)
    make_complete_brief(db, tender_id=tender.id)
    populate_typical_vault(db)
    # First two calls return malformed text; engine retries once then gives
    # up cleanly and stores a structured_content=NULL incomplete draft.
    client = FakeDraftingLLMClient(
        sequential_responses("not json at all", "still not json"),
    )
    agent = DraftingAgent(client)
    outcome = await agent.draft_question(
        db, tender_id=tender.id, org_id=1, request=_make_request("technical_capability")
    )
    assert outcome.draft.status == "incomplete"
    assert outcome.draft.structured_content is None
    assert outcome.draft.error_detail is not None


async def test_first_attempt_malformed_then_valid_succeeds(db) -> None:
    tender = make_tender(db)
    make_complete_brief(db, tender_id=tender.id)
    vault = populate_typical_vault(db)
    good_payload = technical_capability_payload(
        vault_iso=vault["iso_27001"], vault_case_study=vault["case_study"]
    )
    client = FakeDraftingLLMClient(
        sequential_responses("not json", good_payload),
    )
    agent = DraftingAgent(client)
    outcome = await agent.draft_question(
        db, tender_id=tender.id, org_id=1, request=_make_request("technical_capability")
    )
    assert outcome.draft.status == "needs_review"


# ---------------------------------------------------------------------------
# Package wrapper — drafts share one package per (tender, org)
# ---------------------------------------------------------------------------


async def test_drafts_share_one_package_per_tender(db) -> None:
    tender = make_tender(db)
    make_complete_brief(db, tender_id=tender.id)
    vault = populate_typical_vault(db)
    payload = technical_capability_payload(
        vault_iso=vault["iso_27001"], vault_case_study=vault["case_study"]
    )
    agent = DraftingAgent(FakeDraftingLLMClient(static_response(payload)))
    a = await agent.draft_question(
        db, tender_id=tender.id, org_id=1, request=_make_request("technical_capability")
    )
    b = await agent.draft_question(
        db, tender_id=tender.id, org_id=1, request=_make_request("technical_capability")
    )
    assert a.package.id == b.package.id
    packages = db.query(SubmissionPackage).filter_by(tender_id=tender.id).all()
    assert len(packages) == 1
    drafts = db.query(SubmissionQuestionDraft).filter_by(tender_id=tender.id).all()
    assert len(drafts) == 2


# ---------------------------------------------------------------------------
# Templates registry — every template defines what the spec expects
# ---------------------------------------------------------------------------


def test_every_template_carries_required_slots_and_evidence_minimums() -> None:
    for tid, spec in TEMPLATES.items():
        assert spec.required_slots, f"{tid} has no required_slots"
        assert spec.evidence_minimums, f"{tid} has no evidence_minimums"
        assert spec.scoring_check, f"{tid} has no scoring_check rules"
