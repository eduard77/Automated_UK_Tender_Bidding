"""Submission-package draft + read endpoints.

NEVER submits anything. The status enum on the storage layer has no
'submitted' state by design — submission lives on the portal rail
(FetchTask + portal adapters) and is human-gated permanently.

POST /tenders/{id}/submission-package/questions
    Drafts a single question against the requested template and stores
    the result. Drafts are always stored as 'needs_review' (passed
    blocking validators) or 'incomplete' (a blocking validator failed,
    surfaced).

GET  /tenders/{id}/submission-package
    Returns the package + all its question drafts.

GET  /tenders/{id}/submission-package/questions/{draft_id}
    Single draft, full validation_report.

Seams (TODO, deliberately NOT wired this run):
* PAYMENT — generation will sit behind the paid submission-package fee
  (services/billing/, unmerged chunk 6 branch).
* GO/NO-GO — warnings + client self-cert shown BEFORE generation (PR #71).
* SELF-CERT PERSISTENCE — needs the accounts table from chunk 6.
"""
from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from tender_agent.db import get_db
from tender_agent.models import (
    SubmissionPackage,
    SubmissionQuestionDraft,
    Tender,
)
from tender_agent.services.submission.engine import (
    BriefNotReadyForDrafting,
    DraftingAgent,
    DraftRequest,
)
from tender_agent.services.submission.llm_client import (
    AnthropicDraftingLLMClient,
)
from tender_agent.services.submission.templates import CORE_TEMPLATE_IDS

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/tenders", tags=["submission"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class BuyerContext(BaseModel):
    strategy_docs: list[str] = Field(default_factory=list)
    operational_context: str = ""
    priorities: list[str] = Field(default_factory=list)


class DraftQuestionRequest(BaseModel):
    """templates.yaml shared_inputs for one question. Provided per request
    this chunk; auto-extraction from the ITT is deferred."""

    template_id: str
    question_text: str
    question_weight_pct: float
    word_limit: int
    evaluation_criteria: str
    buyer_context: BuyerContext = Field(default_factory=BuyerContext)
    # Per-tenant scope; defaults to the single-org deployment id used
    # elsewhere in the codebase.
    org_id: int = 1


class DraftRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    package_id: int
    tender_id: int
    org_id: int
    template_id: str
    schema_version: str
    question_ref: str
    structured_content: dict | None
    vault_citations: list | None
    confidence_scores: dict | None
    unfilled_slots: list | None
    validation_report: dict | None
    status: str
    model: str | None
    input_tokens: int | None
    output_tokens: int | None
    error_detail: str | None


class PackageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    tender_id: int
    org_id: int
    status: str
    drafts: list[DraftRead] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Injectable agent — tests override this dependency with a fake LLM client.
# ---------------------------------------------------------------------------


def _build_default_agent() -> DraftingAgent:
    return DraftingAgent(llm_client=AnthropicDraftingLLMClient())


def get_drafting_agent() -> DraftingAgent:
    """FastAPI dependency. Tests override to inject a fake LLM client."""
    return _build_default_agent()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post(
    "/{tender_id}/submission-package/questions", response_model=DraftRead
)
async def draft_question(
    tender_id: int,
    body: DraftQuestionRequest,
    db: Session = Depends(get_db),
    agent: DraftingAgent = Depends(get_drafting_agent),
) -> DraftRead:
    """Draft one question. Stored as a DRAFT for human review.

    SEAM (TODO): payment + go/no-go gate. This handler currently drafts
    unconditionally; in production the gate will be checked here once the
    accounts + go/no-go branches land on main.
    """
    tender = db.get(Tender, tender_id)
    if tender is None:
        raise HTTPException(status_code=404, detail="tender_not_found")
    if body.template_id not in CORE_TEMPLATE_IDS:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "template_out_of_scope",
                "supported": list(CORE_TEMPLATE_IDS),
                "message": (
                    "This chunk supports five core text templates only. "
                    "pricing_schedule and case_study are deferred."
                ),
            },
        )

    request = DraftRequest(
        template_id=body.template_id,
        question_text=body.question_text,
        question_weight_pct=body.question_weight_pct,
        word_limit=body.word_limit,
        evaluation_criteria=body.evaluation_criteria,
        buyer_strategy_docs=body.buyer_context.strategy_docs,
        buyer_operational_context=body.buyer_context.operational_context,
        buyer_priorities=body.buyer_context.priorities,
    )

    try:
        outcome = await agent.draft_question(
            db,
            tender_id=tender_id,
            org_id=body.org_id,
            request=request,
        )
    except BriefNotReadyForDrafting:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "brief_not_ready",
                "message": (
                    "Generate a brief for this tender before drafting "
                    "submission responses — the drafting agent reads the "
                    "brief, it doesn't reproduce it."
                ),
            },
        ) from None
    return DraftRead.model_validate(outcome.draft)


@router.get(
    "/{tender_id}/submission-package", response_model=PackageRead | None
)
def get_submission_package(
    tender_id: int,
    org_id: int = 1,
    db: Session = Depends(get_db),
) -> PackageRead | None:
    tender = db.get(Tender, tender_id)
    if tender is None:
        raise HTTPException(status_code=404, detail="tender_not_found")
    package = db.execute(
        select(SubmissionPackage)
        .where(SubmissionPackage.tender_id == tender_id)
        .where(SubmissionPackage.org_id == org_id)
        .order_by(SubmissionPackage.created_at.desc())
    ).scalars().first()
    if package is None:
        return None
    out = PackageRead.model_validate(package)
    out.drafts = [DraftRead.model_validate(d) for d in package.drafts]
    return out


@router.get(
    "/{tender_id}/submission-package/questions/{draft_id}",
    response_model=DraftRead,
)
def get_question_draft(
    tender_id: int,
    draft_id: int,
    db: Session = Depends(get_db),
) -> DraftRead:
    draft = db.get(SubmissionQuestionDraft, draft_id)
    if draft is None or draft.tender_id != tender_id:
        raise HTTPException(status_code=404, detail="draft_not_found")
    return DraftRead.model_validate(draft)


# ---------------------------------------------------------------------------
# Operational note: ANTHROPIC_API_KEY presence is checked at LLM call time
# (services/submission/llm_client.py raises LLMConfigurationError with a
# clean message). We don't preflight here because the same check is needed
# by the brief endpoint; one path keeps the message consistent.
# ---------------------------------------------------------------------------
