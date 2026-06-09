"""Draft a SUGGESTED reply to a tender email — never sent.

Reuses the existing brief LLM client (services/brief/llm_client.py). The output
is a suggestion stored for the user to review, edit, and send themselves. The
system has no send capability anywhere; this module only produces text.

Grounding rules (CLAUDE.md §8): base the draft on the email content + what we
know about the tender. Don't invent commitments, prices, or facts. If the email
is purely informational, return a short acknowledgement or "no reply needed"
rather than a fabricated reply.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

import structlog

from tender_agent.models import Tender
from tender_agent.services.brief.llm_client import (
    AnthropicBriefLLMClient,
    BriefLLMClient,
)
from tender_agent.services.email.providers.base import EmailMessage

logger = structlog.get_logger(__name__)

# Cap how much body we hand the model — enough for context, bounded for cost.
_MAX_BODY_CHARS = 6000
_MAX_OUTPUT_TOKENS = 1200

SYSTEM_PROMPT = (
    "You are an assistant to a UK SME that bids for public-sector contracts. "
    "An email has arrived about a live tender. Draft a SHORT, professional "
    "reply the bid manager could send back, grounded ONLY in the email and the "
    "tender facts provided.\n\n"
    "Strict rules:\n"
    "- Never invent commitments, prices, dates, or facts not present in the "
    "inputs. If something would need a fact you don't have, leave a clearly "
    "marked placeholder like [confirm X] rather than guessing.\n"
    "- If the email is purely informational (e.g. 'documents published', an "
    "automated notification), no reply is usually needed: set reply_needed to "
    "false and make the draft a one-line note explaining why.\n"
    "- Do not quote large blocks of the buyer's text verbatim; paraphrase.\n"
    "- Keep it concise and courteous; the human will review and send it.\n\n"
    "Respond with ONLY a JSON object, no prose, no code fences:\n"
    '{"reply_needed": true|false, "draft": "the suggested reply text", '
    '"reasoning": "one sentence on why"}'
)


@dataclass
class DraftResult:
    # 'drafted' | 'no_reply_needed' | 'error'
    status: str
    draft_text: str
    reasoning: str | None = None


def _build_user_prompt(message: EmailMessage, tender: Tender) -> str:
    ref = tender.procurement_ref or tender.source_ref or ""
    body = (message.body_text or "")[:_MAX_BODY_CHARS]
    return (
        "TENDER\n"
        f"- Title: {tender.title}\n"
        f"- Reference: {ref}\n"
        f"- Buyer: {tender.buyer_name or 'unknown'}\n\n"
        "INBOUND EMAIL\n"
        f"- From: {message.sender}\n"
        f"- Subject: {message.subject}\n"
        f"- Body:\n{body}\n"
    )


def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1] if "\n" in t else t
        if t.endswith("```"):
            t = t[: t.rfind("```")]
    return t.strip()


async def generate_draft(
    message: EmailMessage,
    tender: Tender,
    *,
    llm: BriefLLMClient | None = None,
) -> DraftResult:
    """Produce a suggested-reply draft. Degrades gracefully: any failure yields
    a DraftResult(status="error") with a safe placeholder, never raises."""
    client = llm or AnthropicBriefLLMClient()
    try:
        response = await client.complete(
            system=SYSTEM_PROMPT,
            user=_build_user_prompt(message, tender),
            max_tokens=_MAX_OUTPUT_TOKENS,
        )
    except Exception as exc:  # noqa: BLE001 — drafting must never break the poll
        logger.warning("email.draft_llm_failed", error=str(exc))
        return DraftResult(
            status="error",
            draft_text="",
            reasoning="draft generation failed; review the email manually",
        )

    try:
        parsed = json.loads(_strip_fences(response.text))
        reply_needed = bool(parsed.get("reply_needed", True))
        draft_text = str(parsed.get("draft", "")).strip()
        reasoning = parsed.get("reasoning")
    except (json.JSONDecodeError, AttributeError, TypeError) as exc:
        logger.warning("email.draft_parse_failed", error=str(exc))
        return DraftResult(
            status="error",
            draft_text="",
            reasoning="model returned an unparseable draft",
        )

    status = "drafted" if reply_needed else "no_reply_needed"
    return DraftResult(status=status, draft_text=draft_text, reasoning=reasoning)
