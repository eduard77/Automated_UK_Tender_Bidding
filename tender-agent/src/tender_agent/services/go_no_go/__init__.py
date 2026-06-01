"""Go/No-Go warning engine.

Implements `docs/bid-writing/go-no-go.yaml` — an ADVISORY warning + rating
service. It NEVER blocks payment or generation; it produces warnings,
self-certification questions, and reconciliation notes that the dashboard
surfaces and the client decides what to do with.

Inputs are deterministic:
* the brief row (tender_briefs.brief_json — already produced by the brief
  engine; we never re-call the LLM to recompute what the brief has),
* the vault (vault_documents + vault_document_versions.claims, tenant-scoped).

LLM calls: NONE in the default path. The spec marks LLM scoring as optional;
if a future build wires one in, it must use a mockable client (see
`services.brief.brief_llm_client` for the established pattern).
"""
from tender_agent.services.go_no_go.engine import (  # noqa: F401
    GoNoGoResult,
    assess_tender,
)
from tender_agent.services.go_no_go.reconciliation import (  # noqa: F401
    ReconciliationResult,
    reconcile_vault_against_tender,
)
from tender_agent.services.go_no_go.self_cert import (  # noqa: F401
    SelfCertificationQuestion,
    build_self_certification_questions,
)
