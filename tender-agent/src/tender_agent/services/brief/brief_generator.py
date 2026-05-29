"""Bid-brief generator (chunk 5).

Loads a tender's stored document content, asks Claude for a strict JSON
analysis, validates it, and persists a tender_briefs row. The brief LEADS with
recommendation + key_risks — the human decides whether to bid, but they get a
2-page recommendation-first read first.

Token budgeting picks ITT / instructions / quality-questionnaire / spec
docs first by filename match, truncates large spreadsheet dumps, and records
which docs were included full / truncated / omitted on the brief.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog
from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from tender_agent.models import (
    Tender,
    TenderBrief,
    TenderDocumentContent,
    TenderDocumentFile,
)
from tender_agent.services.brief.content_store import (
    ensure_content_extracted,
    get_content_for_tender,
)
from tender_agent.services.brief.llm_client import (
    BRIEF_LLM_MODEL_DEFAULT,
    AnthropicBriefLLMClient,
    BriefLLMClient,
    LLMConfigurationError,
)

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Prompt (recommendation-led, risks-first)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "You are an expert UK construction bid manager analysing an Invitation to "
    "Tender (ITT) pack on behalf of an SME contractor. You read the documents "
    "and produce a concise, recommendation-led brief that a director can read "
    "in two minutes to decide whether to bid.\n\n"
    "Strict rules:\n"
    "- Only use information actually present in the documents. If a fact is "
    "not stated, return null for that field rather than inventing it.\n"
    "- The brief leads with a BID / NO-BID / CONDITIONAL recommendation and "
    "the top risks. Be decisive but evidence-based.\n"
    "- Identify commercial, legal, programme and resource risks specifically.\n"
    "- Reproduce monetary values, percentages, and dates exactly as stated.\n"
    "- Output STRICT JSON only — no preamble, no markdown fencing.\n"
)


USER_TEMPLATE = """Analyse the following UK construction tender for an SME \
contractor and decide whether they should bid. Return a single JSON object \
matching this schema exactly (use null for unknown fields; do NOT invent \
information that is not in the documents):

{{
  "recommendation": "bid" | "no_bid" | "conditional",
  "confidence": "high" | "medium" | "low",
  "headline": "single-sentence summary of the opportunity and your call",
  "rationale": "2-4 sentences justifying the recommendation",
  "key_risks": [
    {{ "risk": "...", "severity": "high"|"medium"|"low", "detail": "..." }}
  ],
  "deadline": {{ "date": "ISO date or quoted string", "note": "string or null" }},
  "contract_value": {{ "amount": "string or null", "note": "string or null" }},
  "mandatory_requirements": ["..."],
  "scoring": {{
    "summary": "price vs quality split if stated, else null",
    "criteria": [{{ "criterion": "...", "weight": "string or null" }}]
  }},
  "scope_summary": "3-5 sentences in plain English",
  "notable_conditions": ["bonds/retention/PCG/TUPE/unusual terms"],
  "missing_or_unclear": ["things to clarify with the buyer"]
}}

TENDER METADATA
---------------
Title: {title}
Buyer: {buyer}
Value: {value}
Deadline: {deadline}
Source URL: {source_url}
CPV codes: {cpv_codes}

DOCUMENTS
---------
{documents}

Return ONLY the JSON object.
"""


# ---------------------------------------------------------------------------
# Pydantic validation schema
# ---------------------------------------------------------------------------


class _KeyRisk(BaseModel):
    risk: str
    severity: str
    detail: str | None = None

    @field_validator("severity")
    @classmethod
    def _severity_ok(cls, v: str) -> str:
        v_low = v.lower().strip()
        if v_low not in {"high", "medium", "low"}:
            raise ValueError(f"severity must be high/medium/low, got {v!r}")
        return v_low


class _Deadline(BaseModel):
    date: str | None = None
    note: str | None = None


class _ContractValue(BaseModel):
    amount: str | None = None
    note: str | None = None


class _Criterion(BaseModel):
    criterion: str
    weight: str | None = None


class _Scoring(BaseModel):
    summary: str | None = None
    criteria: list[_Criterion] = Field(default_factory=list)


class BriefSchema(BaseModel):
    recommendation: str
    confidence: str
    headline: str
    rationale: str
    key_risks: list[_KeyRisk] = Field(default_factory=list)
    deadline: _Deadline | None = None
    contract_value: _ContractValue | None = None
    mandatory_requirements: list[str] = Field(default_factory=list)
    scoring: _Scoring | None = None
    scope_summary: str | None = None
    notable_conditions: list[str] = Field(default_factory=list)
    missing_or_unclear: list[str] = Field(default_factory=list)

    @field_validator("recommendation")
    @classmethod
    def _rec_ok(cls, v: str) -> str:
        v_low = v.lower().strip()
        if v_low not in {"bid", "no_bid", "conditional"}:
            raise ValueError(
                f"recommendation must be bid/no_bid/conditional, got {v!r}"
            )
        return v_low

    @field_validator("confidence")
    @classmethod
    def _conf_ok(cls, v: str) -> str:
        v_low = v.lower().strip()
        if v_low not in {"high", "medium", "low"}:
            raise ValueError(
                f"confidence must be high/medium/low, got {v!r}"
            )
        return v_low


# ---------------------------------------------------------------------------
# Token budgeting & document assembly
# ---------------------------------------------------------------------------

# Roughly chars-per-token for English-heavy ITT text. The prompt is a few hundred
# tokens, the metadata is small — the doc payload is what matters. Conservative.
_CHARS_PER_TOKEN = 3.5

# Filename keywords that mark "must include first" docs. The matcher is case-
# insensitive and substring-based; precise enough for typical buyer naming.
_PRIORITY_PATTERNS = [
    (re.compile(r"\bitt\b|\binvitation[\s-]*to[\s-]*tender\b", re.I), 100),
    (re.compile(r"instruction", re.I), 90),
    (re.compile(r"quality\s*questionnaire|method\s*statement", re.I), 85),
    (re.compile(r"specification|scope\s*of\s*works|sow\b|requirements?", re.I), 80),
    (re.compile(r"contract\s*conditions?|terms|t&cs|tnc", re.I), 70),
    (re.compile(r"pricing|bill\s*of\s*quantit|boq|price\s*schedule", re.I), 60),
    (re.compile(r"drawings?", re.I), 30),
    (re.compile(r"appendix", re.I), 20),
]


def _priority(filename: str | None, doc_type: str | None) -> int:
    name = (filename or "").lower()
    score = 50  # baseline
    for pat, weight in _PRIORITY_PATTERNS:
        if pat.search(name):
            score = max(score, weight)
    # Penalise spreadsheets unless explicitly priced docs — they balloon.
    if doc_type == "xlsx" and "pricing" not in name and "boq" not in name:
        score -= 10
    return score


@dataclass
class _PreparedDoc:
    filename: str
    sha256: str
    doc_type: str | None
    text: str
    included: str  # "full" | "truncated" | "omitted"
    priority: int


def _budget_chars(budget_tokens: int) -> int:
    return int(budget_tokens * _CHARS_PER_TOKEN)


def _shrink_text(text: str, max_chars: int, doc_type: str | None) -> str:
    """Truncate large bodies. For xlsx we keep the head (sheet titles + a
    sample of rows) since structure carries more meaning than tail rows."""
    if len(text) <= max_chars:
        return text
    if doc_type == "xlsx":
        head = text[: max_chars - 200]
        return head + "\n…[truncated spreadsheet body — structure retained above]"
    head = text[: int(max_chars * 0.85)]
    tail = text[-int(max_chars * 0.10):]
    return f"{head}\n…[truncated]…\n{tail}"


def _select_documents(
    pairs: list[tuple[TenderDocumentFile, TenderDocumentContent]],
    *,
    budget_tokens: int,
) -> list[_PreparedDoc]:
    """Apply the budget: priority order, full where it fits, truncate large
    spreadsheets, omit the lowest-priority overflow."""
    enriched = []
    for f, c in pairs:
        if c.extraction_status != "ok" or not c.extracted_text:
            continue
        enriched.append(
            (
                _priority(f.title or os.path.basename(f.url), c.doc_type),
                f,
                c,
            )
        )
    enriched.sort(key=lambda r: (-r[0], -(r[2].char_count or 0)))

    budget = _budget_chars(budget_tokens)
    used = 0
    out: list[_PreparedDoc] = []
    for prio, f, c in enriched:
        text = c.extracted_text or ""
        name = f.title or os.path.basename(f.url) or f"file-{f.id}"
        if used >= budget:
            out.append(
                _PreparedDoc(
                    filename=name,
                    sha256=c.sha256,
                    doc_type=c.doc_type,
                    text="",
                    included="omitted",
                    priority=prio,
                )
            )
            continue
        remaining = budget - used
        if len(text) <= remaining:
            out.append(
                _PreparedDoc(
                    filename=name,
                    sha256=c.sha256,
                    doc_type=c.doc_type,
                    text=text,
                    included="full",
                    priority=prio,
                )
            )
            used += len(text)
        else:
            # Spreadsheets get a smaller per-doc allowance so they can't drown
            # out an ITT: cap a single xlsx at 25k chars.
            cap = min(remaining, 25_000 if c.doc_type == "xlsx" else remaining)
            shrunk = _shrink_text(text, cap, c.doc_type)
            out.append(
                _PreparedDoc(
                    filename=name,
                    sha256=c.sha256,
                    doc_type=c.doc_type,
                    text=shrunk,
                    included="truncated",
                    priority=prio,
                )
            )
            used += len(shrunk)
    return out


def _format_documents_block(docs: list[_PreparedDoc]) -> str:
    blocks: list[str] = []
    for d in docs:
        if d.included == "omitted":
            continue
        suffix = " (TRUNCATED)" if d.included == "truncated" else ""
        blocks.append(f"=== {d.filename}{suffix} ===\n{d.text}")
    return "\n\n".join(blocks) or "(no document content available)"


def _format_value(t: Tender) -> str:
    if t.value_amount is None:
        return "not stated"
    return f"{t.value_currency or ''} {t.value_amount}".strip()


def _format_deadline(t: Tender) -> str:
    return t.deadline_at.isoformat() if t.deadline_at else "not stated"


def _build_user_prompt(tender: Tender, docs: list[_PreparedDoc]) -> str:
    return USER_TEMPLATE.format(
        title=tender.title or "(untitled)",
        buyer=tender.buyer_name or "(unknown)",
        value=_format_value(tender),
        deadline=_format_deadline(tender),
        source_url=tender.source_url or "(none)",
        cpv_codes=", ".join(tender.cpv_codes or []) or "(none)",
        documents=_format_documents_block(docs),
    )


def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1] if "\n" in t else t
        if t.endswith("```"):
            t = t[: t.rfind("```")]
    return t.strip()


def _parse_and_validate(raw: str) -> BriefSchema:
    text = _strip_fences(raw)
    parsed: Any = json.loads(text)
    return BriefSchema.model_validate(parsed)


def _max_input_tokens() -> int:
    raw = os.environ.get("BRIEF_MAX_INPUT_TOKENS")
    if raw:
        try:
            n = int(raw)
            if n > 0:
                return n
        except ValueError:
            pass
    return 150_000


def _max_output_tokens() -> int:
    return 4096


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def generate_brief(
    db: Session,
    tender_id: int,
    *,
    llm: BriefLLMClient | None = None,
) -> TenderBrief:
    """Produce a brief for `tender_id`. Persists a tender_briefs row regardless
    of outcome (status=complete / failed). Never raises into the caller — the
    background task wraps this and treats any exception as a failed brief, but
    in normal operation this function handles its own errors."""
    tender = db.get(Tender, tender_id)
    if tender is None:
        raise ValueError(f"tender {tender_id} not found")

    now = datetime.now(UTC)
    brief = TenderBrief(
        tender_id=tender_id,
        status="generating",
        model=(llm.model if llm else BRIEF_LLM_MODEL_DEFAULT),
        created_at=now,
        updated_at=now,
    )
    db.add(brief)
    db.commit()
    db.refresh(brief)

    try:
        # Idempotent safety net: ensure content is extracted even if fetch ran
        # before this code shipped.
        ensure_content_extracted(db, tender)
        pairs = get_content_for_tender(db, tender_id)
        if not pairs:
            return _fail(
                db,
                brief,
                "No documents to analyse — fetch documents first.",
                documents_considered=[],
            )

        prepared = _select_documents(pairs, budget_tokens=_max_input_tokens())
        if not any(p.included != "omitted" for p in prepared):
            return _fail(
                db,
                brief,
                "All documents were unreadable or empty — re-fetch the pack.",
                documents_considered=[
                    {"filename": p.filename, "included": p.included}
                    for p in prepared
                ],
            )

        documents_considered = [
            {
                "filename": p.filename,
                "included": p.included,
                "sha256": p.sha256,
                "doc_type": p.doc_type,
            }
            for p in prepared
        ]
        brief.documents_considered = documents_considered
        db.commit()

        client = llm or AnthropicBriefLLMClient()
        user_prompt = _build_user_prompt(tender, prepared)

        try:
            response = await client.complete(
                system=SYSTEM_PROMPT,
                user=user_prompt,
                max_tokens=_max_output_tokens(),
            )
        except LLMConfigurationError as exc:
            return _fail(db, brief, str(exc), documents_considered=documents_considered)
        except Exception as exc:  # noqa: BLE001
            return _fail(
                db,
                brief,
                f"LLM call failed: {type(exc).__name__}: {exc}",
                documents_considered=documents_considered,
            )

        try:
            validated = _parse_and_validate(response.text)
        except (json.JSONDecodeError, ValidationError) as exc:
            logger.warning(
                "brief.malformed_json_retry",
                tender_id=tender_id,
                error=str(exc),
            )
            # One stricter retry — append explicit instruction to the user
            # prompt so the model corrects the previous output.
            retry_user = (
                user_prompt
                + "\n\nIMPORTANT: Your previous response was not valid JSON "
                "matching the schema. Return ONLY a single JSON object that "
                "matches the schema exactly — no markdown, no commentary."
            )
            try:
                response = await client.complete(
                    system=SYSTEM_PROMPT,
                    user=retry_user,
                    max_tokens=_max_output_tokens(),
                )
                validated = _parse_and_validate(response.text)
            except (json.JSONDecodeError, ValidationError) as exc2:
                return _fail(
                    db,
                    brief,
                    f"LLM returned invalid JSON twice: {exc2}",
                    documents_considered=documents_considered,
                    raw_response=response.text,
                )

        brief.brief_json = validated.model_dump()
        brief.recommendation = validated.recommendation
        brief.confidence = validated.confidence
        brief.headline = validated.headline
        brief.input_tokens = response.input_tokens
        brief.output_tokens = response.output_tokens
        brief.model = response.model
        brief.status = "complete"
        brief.generated_at = datetime.now(UTC)
        brief.updated_at = brief.generated_at
        db.commit()
        db.refresh(brief)
        logger.info(
            "brief.generated",
            tender_id=tender_id,
            recommendation=brief.recommendation,
            confidence=brief.confidence,
            input_tokens=brief.input_tokens,
            output_tokens=brief.output_tokens,
            documents=len([d for d in documents_considered if d["included"] != "omitted"]),
            omitted=len([d for d in documents_considered if d["included"] == "omitted"]),
        )
        return brief
    except Exception as exc:  # noqa: BLE001
        logger.exception("brief.unexpected_failure", tender_id=tender_id)
        return _fail(
            db, brief, f"Unexpected failure: {type(exc).__name__}: {exc}"
        )


def _fail(
    db: Session,
    brief: TenderBrief,
    message: str,
    *,
    documents_considered: list[dict] | None = None,
    raw_response: str | None = None,
) -> TenderBrief:
    brief.status = "failed"
    brief.error_detail = message
    if documents_considered is not None:
        brief.documents_considered = documents_considered
    if raw_response is not None:
        # Truncate so a runaway model output doesn't blow up the row.
        brief.error_detail = (
            f"{message}\nraw[:1000]={raw_response[:1000]}"
        )
    brief.generated_at = datetime.now(UTC)
    brief.updated_at = brief.generated_at
    db.commit()
    db.refresh(brief)
    logger.info(
        "brief.generation_failed",
        tender_id=brief.tender_id,
        error=message,
    )
    return brief


def latest_brief(db: Session, tender_id: int) -> TenderBrief | None:
    return db.execute(
        select(TenderBrief)
        .where(TenderBrief.tender_id == tender_id)
        .order_by(TenderBrief.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
