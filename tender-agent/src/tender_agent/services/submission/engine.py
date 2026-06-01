"""Drafting agent orchestrator.

Per AGENT_SYSTEM_PROMPT.md's reading order: load template + cross-cutting
rules → read criteria → read buyer context (brief + content) → query
feedback streams (skip gracefully if absent) → review evidence candidates
→ check cross-section state → draft → self-check → store as a DRAFT.

NO ENDPOINT IN THIS MODULE SUBMITS ANYTHING. The status enum has no
'submitted' value — drafts come out as 'needs_review' (passed blocking
validators) or 'incomplete' (blocking validator failed, surfaced for
the reviewer).
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from tender_agent.models import (
    SubmissionPackage,
    SubmissionQuestionDraft,
    Tender,
    TenderBrief,
    TenderDocumentContent,
)
from tender_agent.services.submission.evidence import (
    EvidenceCandidate,
    fetch_evidence_candidates,
    fetch_existing_drafts_for_consistency,
)
from tender_agent.services.submission.llm_client import (
    SUBMISSION_PROMPT_BUDGET_TOKENS,
    DraftingLLMClient,
)
from tender_agent.services.submission.templates import (
    CORE_TEMPLATE_IDS,
    SCHEMA_VERSION,
    TemplateSpec,
    expected_response_shape,
    get_template,
)
from tender_agent.services.submission.validators import (
    ValidationReport,
    run_validators,
)

logger = structlog.get_logger(__name__)


class BriefNotReadyForDrafting(Exception):  # noqa: N818 — domain-condition name
    """Raised when a draft is requested for a tender with no complete brief.
    The drafting agent reads the brief; it does not reproduce it. The API
    converts this to a clean 409 state, never a 500."""


class DraftingLLMResponseInvalid(Exception):  # noqa: N818 — domain-condition name
    """The LLM returned text that we couldn't parse as schema-valid JSON
    after one retry. The engine stores nothing broken and surfaces a
    clean failure."""


@dataclass
class DraftRequest:
    """The per-question inputs (templates.yaml shared_inputs)."""

    template_id: str
    question_text: str
    question_weight_pct: float
    word_limit: int
    evaluation_criteria: str
    buyer_strategy_docs: list[str] = field(default_factory=list)
    buyer_operational_context: str = ""
    buyer_priorities: list[str] = field(default_factory=list)


@dataclass
class DraftOutcome:
    """Engine output. The draft row is always written (even on incomplete
    status) so the reviewer can see what the agent produced and why."""

    draft: SubmissionQuestionDraft
    package: SubmissionPackage
    validation_report: ValidationReport


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class DraftingAgent:
    """The drafting agent. Stateless apart from the injected LLM client and
    a fixed prompt budget — re-entrant across requests."""

    def __init__(
        self,
        llm_client: DraftingLLMClient,
        *,
        prompt_budget_tokens: int = SUBMISSION_PROMPT_BUDGET_TOKENS,
    ) -> None:
        self._llm = llm_client
        self._budget = prompt_budget_tokens

    async def draft_question(
        self,
        db: Session,
        *,
        tender_id: int,
        org_id: int,
        request: DraftRequest,
    ) -> DraftOutcome:
        """Draft a single question; persist a SubmissionQuestionDraft row.

        Failure modes (all stored cleanly, never a 500):
          * BriefNotReadyForDrafting — caller must generate a brief first.
          * DraftingLLMResponseInvalid — LLM produced unparsable JSON twice
            in a row; we store a draft with status='incomplete' carrying
            the error_detail and an empty structured_content.
        """
        if request.template_id not in CORE_TEMPLATE_IDS:
            raise ValueError(
                f"template_id {request.template_id!r} is out of scope for "
                f"this chunk; supported: {CORE_TEMPLATE_IDS!r}"
            )
        template = get_template(request.template_id)

        tender = db.get(Tender, tender_id)
        if tender is None:
            raise ValueError(f"tender {tender_id} not found")

        brief = _latest_complete_brief(db, tender_id)
        if brief is None:
            raise BriefNotReadyForDrafting(tender_id)

        # 1. Find-or-create the package — one package per tender per org.
        package = _find_or_create_package(db, tender_id=tender_id, org_id=org_id)

        # 2. Buyer context.
        document_excerpts = _gather_document_excerpts(db, tender_id, org_id)
        candidates = fetch_evidence_candidates(
            db, org_id=org_id, template_id=request.template_id
        )
        cross_state = fetch_existing_drafts_for_consistency(
            db, package_id=package.id, org_id=org_id
        )

        # 3. Build prompt + ask LLM.
        system_prompt = _build_system_prompt(template)
        user_prompt = _build_user_prompt(
            template=template,
            request=request,
            brief=brief,
            document_excerpts=document_excerpts,
            candidates=candidates,
            cross_section_state=cross_state,
            budget_tokens=self._budget,
        )

        llm_text, model_used, tokens_in, tokens_out, error_detail = (
            await self._invoke_llm(system_prompt, user_prompt)
        )
        if error_detail is not None:
            draft = _store_invalid_draft(
                db,
                package=package,
                tender_id=tender_id,
                org_id=org_id,
                request=request,
                question_ref=_question_ref(request.question_text),
                error_detail=error_detail,
                model=model_used,
            )
            empty_report = ValidationReport(is_blocking_failure=True)
            return DraftOutcome(
                draft=draft, package=package, validation_report=empty_report
            )

        # 4. Parse + validate.
        parsed = _parse_llm_json(llm_text)
        structured_content = parsed.get("structured_content") or {}
        vault_citations = parsed.get("vault_citations") or []
        confidence_scores = parsed.get("confidence_scores") or {}
        unfilled_slots = parsed.get("unfilled_slots") or []

        report = run_validators(
            template=template,
            structured_content=structured_content,
            vault_citations=vault_citations,
            confidence_scores=confidence_scores,
            unfilled_slots=unfilled_slots,
            cross_section_state=cross_state,
            itt_question_text=request.question_text,
        )

        # 5. Persist as a draft (NEVER 'submitted'; only review states).
        status = "incomplete" if report.is_blocking_failure else "needs_review"
        draft = SubmissionQuestionDraft(
            package_id=package.id,
            tender_id=tender_id,
            org_id=org_id,
            template_id=request.template_id,
            schema_version=SCHEMA_VERSION,
            question_ref=_question_ref(request.question_text),
            structured_content=structured_content,
            vault_citations=vault_citations,
            confidence_scores=confidence_scores,
            unfilled_slots=unfilled_slots,
            validation_report=report.to_dict(),
            status=status,
            model=model_used,
            input_tokens=tokens_in,
            output_tokens=tokens_out,
        )
        db.add(draft)
        package.updated_at = datetime.now(UTC)
        # The package status is 'drafting' until the human marks it review-
        # ready; this engine never auto-flips to 'needs_review' on the
        # package row (per spec: no auto-state-progress, surface to human).
        db.commit()
        db.refresh(draft)
        logger.info(
            "submission.draft_persisted",
            draft_id=draft.id,
            package_id=package.id,
            tender_id=tender_id,
            template_id=request.template_id,
            status=status,
            blocking=report.is_blocking_failure,
        )
        return DraftOutcome(draft=draft, package=package, validation_report=report)

    # ------------------------------------------------------------------ #
    # LLM
    # ------------------------------------------------------------------ #

    async def _invoke_llm(
        self, system: str, user: str
    ) -> tuple[str, str, int | None, int | None, str | None]:
        """Try twice (initial + 1 stricter retry). Returns
        (text, model, in_tokens, out_tokens, error). error is None on
        success."""
        try:
            response = await self._llm.complete(
                system=system, user=user, max_tokens=8000
            )
            try:
                _parse_llm_json(response.text)
            except DraftingLLMResponseInvalid:
                # Retry once with a stricter reminder.
                stricter_user = (
                    user
                    + "\n\nIMPORTANT: Your previous response wasn't valid "
                    "JSON. Return ONE JSON object only — no prose, no "
                    "markdown fence."
                )
                response = await self._llm.complete(
                    system=system, user=stricter_user, max_tokens=8000
                )
                _parse_llm_json(response.text)  # raises if still bad
            return (
                response.text,
                response.model,
                response.input_tokens,
                response.output_tokens,
                None,
            )
        except DraftingLLMResponseInvalid as exc:
            return ("", getattr(self._llm, "model", "unknown"), None, None, str(exc))
        except Exception as exc:  # noqa: BLE001
            logger.warning("submission.llm_error", error=str(exc))
            return ("", getattr(self._llm, "model", "unknown"), None, None, str(exc))


# ---------------------------------------------------------------------------
# Helpers — DB
# ---------------------------------------------------------------------------


def _latest_complete_brief(db: Session, tender_id: int) -> TenderBrief | None:
    return db.execute(
        select(TenderBrief)
        .where(TenderBrief.tender_id == tender_id)
        .where(TenderBrief.status == "complete")
        .order_by(TenderBrief.created_at.desc())
    ).scalars().first()


def _find_or_create_package(
    db: Session, *, tender_id: int, org_id: int
) -> SubmissionPackage:
    row = db.execute(
        select(SubmissionPackage)
        .where(SubmissionPackage.tender_id == tender_id)
        .where(SubmissionPackage.org_id == org_id)
        .order_by(SubmissionPackage.created_at.desc())
    ).scalars().first()
    if row is not None:
        return row
    pkg = SubmissionPackage(
        tender_id=tender_id, org_id=org_id, status="drafting"
    )
    db.add(pkg)
    db.commit()
    db.refresh(pkg)
    return pkg


def _gather_document_excerpts(
    db: Session, tender_id: int, org_id: int  # noqa: ARG001 — content is per-tender, not org-scoped
) -> list[dict[str, Any]]:
    """A small excerpt of the ITT-extracted content. Used by the prompt so
    the agent can ground claims in the buyer's own language. Truncated to
    keep the prompt within budget; full text is on the document content
    rows for the human reviewer."""
    rows = db.execute(
        select(TenderDocumentContent)
        .where(TenderDocumentContent.tender_id == tender_id)
        .where(TenderDocumentContent.extraction_status == "ok")
        .order_by(TenderDocumentContent.created_at.desc())
        .limit(3)
    ).scalars().all()
    out: list[dict[str, Any]] = []
    for row in rows:
        text = row.extracted_text or ""
        out.append(
            {
                "content_id": row.id,
                "doc_type": row.doc_type,
                "char_count": len(text),
                "excerpt": text[:2000],
            }
        )
    return out


def _store_invalid_draft(
    db: Session,
    *,
    package: SubmissionPackage,
    tender_id: int,
    org_id: int,
    request: DraftRequest,
    question_ref: str,
    error_detail: str,
    model: str,
) -> SubmissionQuestionDraft:
    draft = SubmissionQuestionDraft(
        package_id=package.id,
        tender_id=tender_id,
        org_id=org_id,
        template_id=request.template_id,
        schema_version=SCHEMA_VERSION,
        question_ref=question_ref,
        structured_content=None,
        vault_citations=None,
        confidence_scores=None,
        unfilled_slots=None,
        validation_report={"is_blocking_failure": True, "error": error_detail},
        status="incomplete",
        model=model,
        error_detail=error_detail,
    )
    db.add(draft)
    db.commit()
    db.refresh(draft)
    return draft


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT_PATH = (
    Path(__file__).resolve().parents[5]  # repo root, see _agent_system_prompt note
    / "docs"
    / "bid-writing"
    / "AGENT_SYSTEM_PROMPT.md"
)


def _agent_system_prompt() -> str:
    """Read AGENT_SYSTEM_PROMPT.md from disk every call. Cheap (the file is
    ~700 lines) and means the operator can edit the prompt without a
    restart. If the file is missing we fall back to a minimal stub that
    still enforces the hard constraints — never silently ship an empty
    system prompt."""
    try:
        return _SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.warning(
            "submission.system_prompt_missing", path=str(_SYSTEM_PROMPT_PATH)
        )
        return (
            "UK public-sector bid drafting agent.\n"
            "HARD CONSTRAINTS: tenant-isolated, no invention, no "
            "auto-submission, schema-pinned to templates.yaml 1.6, no "
            "verbatim ITT runs >15 words, every claim cites a "
            "VaultDocumentVersion id."
        )


def _build_system_prompt(template: TemplateSpec) -> str:
    base = _agent_system_prompt()
    shape = json.dumps(expected_response_shape(template), indent=2)
    return (
        base
        + "\n\n---\n\n## TEMPLATE PIN\n\n"
        + f"template_id: {template.template_id}\n"
        + f"schema_version: {SCHEMA_VERSION}\n\n"
        + f"description: {template.description}\n\n"
        + "Required top-level slots in structured_content: "
        + ", ".join(template.required_slots)
        + "\n\n"
        + "## EXPECTED OUTPUT SHAPE\n\n"
        + f"```json\n{shape}\n```\n\n"
        + "Return EXACTLY ONE JSON object — no prose, no markdown fence."
    )


def _build_user_prompt(
    *,
    template: TemplateSpec,
    request: DraftRequest,
    brief: TenderBrief,
    document_excerpts: list[dict[str, Any]],
    candidates: list[EvidenceCandidate],
    cross_section_state: list[dict[str, Any]],
    budget_tokens: int,
) -> str:
    """Composite user prompt. Token budgeting is rough — we trim evidence
    excerpts before brief / candidates because the LLM benefits most from
    the candidates + criteria."""
    candidate_payload = [c.to_dict() for c in candidates]
    brief_json = brief.brief_json or {}
    brief_summary = {
        "recommendation": brief_json.get("recommendation"),
        "scope_summary": brief_json.get("scope_summary"),
        "headline": brief_json.get("headline"),
        "mandatory_requirements": brief_json.get("mandatory_requirements") or [],
        "key_risks": brief_json.get("key_risks") or [],
    }

    payload = {
        "template_id": template.template_id,
        "schema_version": SCHEMA_VERSION,
        "shared_inputs": {
            "question_text": request.question_text,
            "question_weight_pct": request.question_weight_pct,
            "word_limit": request.word_limit,
            "evaluation_criteria": request.evaluation_criteria,
            "buyer_context": {
                "strategy_docs": request.buyer_strategy_docs,
                "operational_context": request.buyer_operational_context,
                "priorities": request.buyer_priorities,
            },
        },
        "brief_summary": brief_summary,
        "itt_excerpts": document_excerpts,
        "evidence_candidates": candidate_payload,
        "cross_section_state": cross_section_state,
        "feedback_streams_status": {
            "bid_pricing_history": "unmodelled — table not present",
            "bid_feedback_record": "unmodelled — table not present",
        },
        "instructions": (
            "Draft the response against the pinned template. Cite only "
            "from evidence_candidates by vault_version_id. Where you "
            "cannot find evidence for a required slot, return null + an "
            "unfilled_slots entry; NEVER fabricate. Output one JSON "
            "object only."
        ),
    }
    rendered = json.dumps(payload, indent=2, default=str)
    # Truncation guard — characters as a proxy for tokens (~4 chars/token).
    max_chars = budget_tokens * 4
    if len(rendered) > max_chars:
        # Truncate ITT excerpts first.
        payload["itt_excerpts"] = [
            {**e, "excerpt": (e.get("excerpt") or "")[:500]}
            for e in document_excerpts
        ]
        rendered = json.dumps(payload, indent=2, default=str)
        if len(rendered) > max_chars:
            # Then evidence_candidates: trim count.
            payload["evidence_candidates"] = candidate_payload[:10]
            rendered = json.dumps(payload, indent=2, default=str)
    return rendered


# ---------------------------------------------------------------------------
# JSON parse
# ---------------------------------------------------------------------------


def _parse_llm_json(text: str) -> dict[str, Any]:
    """The LLM is asked for one JSON object. Tolerates surrounding text /
    markdown fences (some models still insert them); raises a
    DraftingLLMResponseInvalid if no parseable object is found."""
    if not text or not text.strip():
        raise DraftingLLMResponseInvalid("empty response")
    # Strip code fences if present.
    stripped = text.strip()
    fence_match = re.search(
        r"```(?:json)?\s*(\{.*?\})\s*```", stripped, flags=re.DOTALL
    )
    if fence_match:
        stripped = fence_match.group(1)
    # Find the first top-level JSON object.
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    # Last resort — extract the largest braced span.
    brace_match = re.search(r"\{.*\}", stripped, flags=re.DOTALL)
    if brace_match:
        try:
            return json.loads(brace_match.group(0))
        except json.JSONDecodeError as exc:
            raise DraftingLLMResponseInvalid(str(exc)) from exc
    raise DraftingLLMResponseInvalid("no JSON object found in LLM response")


def _question_ref(question_text: str) -> str:
    """Short hash + first-30-chars label. We store this rather than the
    verbatim ITT text (copyright hard constraint)."""
    digest = hashlib.sha256(question_text.encode("utf-8")).hexdigest()[:10]
    label = (question_text.strip().splitlines() or [""])[0][:30]
    return f"{digest}::{label}"
