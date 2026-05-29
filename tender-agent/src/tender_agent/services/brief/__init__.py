"""Bid-brief engine (chunk 5).

Stages:
 1. document_extractor  — bytes on disk → plain text per file (per-doc status).
 2. content_store       — write/reuse tender_document_content rows by sha256.
 3. llm_client          — mockable Anthropic Messages wrapper (env-keyed).
 4. brief_generator     — assemble stored content, ask Claude, validate JSON,
                          persist a tender_briefs row.

Public entry points used by the API and the fetch orchestrator:
    ensure_content_extracted(db, tender) -> ContentExtractionSummary
    generate_brief(db, tender_id, llm=None) -> TenderBrief
"""
from tender_agent.services.brief.content_store import (
    EXTRACTOR_VERSION,
    ContentExtractionSummary,
    ensure_content_extracted,
)
from tender_agent.services.brief.llm_client import (
    BRIEF_LLM_MODEL_DEFAULT,
    AnthropicBriefLLMClient,
    BriefLLMClient,
    LLMConfigurationError,
)

__all__ = [
    "BRIEF_LLM_MODEL_DEFAULT",
    "EXTRACTOR_VERSION",
    "AnthropicBriefLLMClient",
    "BriefLLMClient",
    "ContentExtractionSummary",
    "LLMConfigurationError",
    "ensure_content_extracted",
]
