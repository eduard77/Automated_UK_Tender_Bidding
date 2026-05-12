"""Tender requirements extractor.

Reads aggregated tender document text + metadata, asks Claude to produce a
structured requirements brief: mandatory requirements, evaluation criteria,
documents required, questions to answer, risk flags, and a go/no-go
recommendation.
"""
from __future__ import annotations

import json
from typing import Any

import structlog
from anthropic import Anthropic
from sqlalchemy.orm import Session

from tender_agent.config import settings
from tender_agent.models import Tender, TenderRequirements
from tender_agent.services.document_downloader import aggregate_text

logger = structlog.get_logger(__name__)

SYSTEM_PROMPT = """You are an experienced UK public-sector bid manager. You analyse \
tender documents and produce structured, factual briefs for a bid team.

Strict rules:
- Only include information actually present in the provided materials. If the source \
doesn't say it, do not invent it.
- For monetary amounts, dates, and quoted text, use the exact form from the source.
- Mark ambiguity explicitly with `"confidence": "low"` rather than guessing.
- If the documents are too sparse to produce a useful brief, say so in the summary \
and set recommendation to "review".
"""

USER_TEMPLATE = """Analyse the following UK public tender and return a strict JSON \
object matching this schema:

{{
  "summary": string,                         // 2-4 sentence plain-English overview
  "evaluation_criteria": [                   // weighted scoring criteria
    {{ "criterion": string, "weight_pct": number|null, "notes": string|null }}
  ],
  "mandatory_requirements": [
    {{ "id": string, "requirement": string, "evidence_needed": string|null,
       "confidence": "high"|"medium"|"low" }}
  ],
  "desired_requirements": [
    {{ "id": string, "requirement": string, "weight": string|null }}
  ],
  "documents_required": [
    {{ "name": string, "kind": string, "notes": string|null }}
  ],
  "questions_to_answer": [
    {{ "id": string, "question": string, "word_limit": number|null, "weight": string|null }}
  ],
  "risk_flags": [string],                    // e.g. "tight deadline", "unusual T&Cs"
  "estimated_effort_days": number|null,      // realistic full-time bid effort
  "recommendation": "pursue"|"decline"|"review",
  "recommendation_reason": string
}}

TENDER METADATA
---------------
Title: {title}
Buyer: {buyer}
Value: {value}
Deadline: {deadline}
Source URL: {source_url}
CPV codes: {cpv_codes}

DOCUMENT EXCERPTS
-----------------
{documents}

Return ONLY the JSON object, no preamble, no markdown fencing.
"""


def _format_value(t: Tender) -> str:
    if t.value_amount is None:
        return "not stated"
    return f"{t.value_currency or ''} {t.value_amount}".strip()


def _format_deadline(t: Tender) -> str:
    return t.deadline_at.isoformat() if t.deadline_at else "not stated"


def _build_prompt(tender: Tender, docs_text: str) -> str:
    return USER_TEMPLATE.format(
        title=tender.title or "(untitled)",
        buyer=tender.buyer_name or "(unknown)",
        value=_format_value(tender),
        deadline=_format_deadline(tender),
        source_url=tender.source_url or "(none)",
        cpv_codes=", ".join(tender.cpv_codes or []) or "(none)",
        documents=docs_text or "(no extractable text from attached documents)",
    )


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.endswith("```"):
            text = text[: text.rfind("```")]
    return text.strip()


def extract_requirements(db: Session, tender: Tender) -> TenderRequirements | None:
    """Run Claude over a tender's documents and persist a TenderRequirements row."""
    if not settings.anthropic_api_key:
        logger.warning("requirements.skipped_no_api_key", tender_id=tender.id)
        return None

    docs_text = aggregate_text(tender.document_files or [])
    if not docs_text and not tender.description:
        logger.info("requirements.no_content", tender_id=tender.id)
        return None

    if not docs_text and tender.description:
        docs_text = f"=== Description ===\n{tender.description}"

    client = Anthropic(api_key=settings.anthropic_api_key)
    try:
        response = client.messages.create(
            model=settings.anthropic_model,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": _build_prompt(tender, docs_text)}],
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("requirements.api_error", tender_id=tender.id, error=str(exc))
        return None

    text_blocks = [b.text for b in response.content if getattr(b, "type", None) == "text"]
    raw = "\n".join(text_blocks)

    try:
        parsed: dict[str, Any] = json.loads(_strip_fences(raw))
    except json.JSONDecodeError as exc:
        logger.error("requirements.parse_failed", tender_id=tender.id, error=str(exc))
        return None

    existing = (
        db.query(TenderRequirements).filter(TenderRequirements.tender_id == tender.id).one_or_none()
    )
    record = existing or TenderRequirements(tender_id=tender.id)

    record.summary = parsed.get("summary")
    record.evaluation_criteria = parsed.get("evaluation_criteria") or []
    record.mandatory_requirements = parsed.get("mandatory_requirements") or []
    record.desired_requirements = parsed.get("desired_requirements") or []
    record.documents_required = parsed.get("documents_required") or []
    record.questions_to_answer = parsed.get("questions_to_answer") or []
    record.risk_flags = parsed.get("risk_flags") or []
    record.estimated_effort_days = parsed.get("estimated_effort_days")
    record.recommendation = parsed.get("recommendation")
    record.recommendation_reason = parsed.get("recommendation_reason")
    record.model = settings.anthropic_model
    record.raw_response = parsed

    if existing is None:
        db.add(record)
    db.commit()
    db.refresh(record)

    logger.info(
        "requirements.extracted",
        tender_id=tender.id,
        recommendation=record.recommendation,
        mandatory=len(record.mandatory_requirements or []),
        questions=len(record.questions_to_answer or []),
    )
    return record
