"""Submission-package generator (drafting spine).

Implements the agent described in `docs/bid-writing/AGENT_SYSTEM_PROMPT.md`
and the per-question schemas in `docs/bid-writing/templates.yaml` v1.6 for
the FIVE core text templates:

    technical_capability
    methodology_delivery
    social_value
    quality_management
    risk_contingency

Out of scope (defer to later chunks): pricing_schedule, case_study + the
visual / Gantt / case-study-generator subsystems, BidPricingHistory and
BidFeedbackRecord tables, auto-extraction of questions from the ITT.

Hard constraints (enforced in code, not just prompt):
* tenant isolation (org_id filter on every vault/brief/content read);
* no invention (unfilled_slots, not fabricated content);
* no auto-submit (the storage layer has no 'submitted' state);
* schema pinning (templates.yaml 1.6);
* copyright (paraphrase the ITT; the engine refuses verbatim runs >15 words).

All LLM access is via an injectable client; tests use a fake returning
canned, schema-valid JSON. ZERO real API calls in CI.
"""
from tender_agent.services.submission.engine import (  # noqa: F401
    BriefNotReadyForDrafting,
    DraftingAgent,
    DraftOutcome,
    DraftRequest,
)
from tender_agent.services.submission.llm_client import (  # noqa: F401
    DraftingLLMClient,
    LLMResponse,
)
