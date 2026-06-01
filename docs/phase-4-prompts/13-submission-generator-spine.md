TASK: Submission-package generator — the drafting SPINE (warnings-philosophy, human-gated, mockable LLM)

GOAL (plain English)
Build the engine that DRAFTS bid responses. Given a tender that has a brief, and a tender QUESTION (its text + evaluation criteria + word limit + weight), the engine drafts a structured, evidence-cited response that is designed to score 5/5, self-checks it against the rules, and stores it as a DRAFT for human review. It never submits anything. This is the core product piece. Scope this run to the SPINE only — prove it drafts real, cited, validated responses for the core text question-types — and defer the heavy subsystems (listed under OUT below) to later chunks.

READ FIRST — these three files are the engine's BRAIN. The code implements them; it does not reinvent them.
- docs/bid-writing/AGENT_SYSTEM_PROMPT.md  — the agent's invariant instructions + hard constraints. Load its content as the LLM system prompt at runtime.
- docs/bid-writing/templates.yaml (schema 1.6) — the per-question response schema, evidence_minimums, scoring_check, failure_modes, cross_cutting_rules. This is the spec the draft must satisfy.
- docs/bid-writing/go-no-go.yaml — referenced only for the pre-generation gate SEAM (see SEAMS). Do not reimplement it.
If any of these three files is missing, BLOCKED: commit them first.

DEPENDENCY GUARD (the phantom-merge trap)
- git pull origin main FIRST. Build from current main.
- Verify on main, and BLOCKED with the exact missing symbol + which branch likely has it, if absent:
  * tender_briefs model/table (the engine reads the brief; it does NOT regenerate it)
  * tender_document_content (extracted ITT content)
  * the vault models (vault_documents / vault_document_versions)
  * the brief engine's Anthropic client (reuse it — do NOT write a second LLM client)
- The go/no-go engine from PR #71 is on an UNMERGED branch. Do NOT import it. The go/no-go gate is a SEAM (below), not a dependency.

SCOPE — IN (build this run)
1. Orchestrator that drafts ONE question at a time per AGENT_SYSTEM_PROMPT.md's reading order, and a package wrapper that groups a tender's drafts.
2. Drafting for the FIVE core text templates only: technical_capability, methodology_delivery, social_value, quality_management, risk_contingency.
3. Vault evidence retrieval: for the tenant (org_id filtered), surface candidate vault items (case studies, certificates, CVs, past responses) with their extracted facts + expiry + relevance, and pass them to the LLM as the ONLY evidence it may cite. Implement the templates.yaml relevance rules where they apply (e.g. six-axis case-study match, cert expiry check). Keep retrieval pragmatic — provide candidates, let the agent select + cite vault IDs.
4. The six self-checks from AGENT_SYSTEM_PROMPT.md LAYER 4, producing a validation_report: evidence_minimums, scoring_check rules, failure_mode patterns, cross-section consistency, language rules, confidence summary.
5. Storage + migration (below).
6. API endpoints (below).
7. Mockable LLM + tests (below).

SCOPE — OUT (explicitly DEFER to later chunks; do NOT build now)
- pricing_schedule template (needs BidPricingHistory + cost data we don't have yet)
- case_study template AND the case-study VISUAL generator, brand_foundation, layout-fingerprint subsystem
- the Gantt / schedule subsystem (schedule-schema.yaml, frappe-gantt)
- auto-extraction of questions from the ITT (questions are PROVIDED as input this run)
- BidPricingHistory / BidFeedbackRecord tables (do not create; see "feedback streams" below)
Leave clearly-marked seams where these will attach.

INPUTS (provided per question — do NOT auto-extract from the ITT this run)
- Per templates.yaml shared_inputs: question_text, question_weight_pct, word_limit, evaluation_criteria, buyer_context (strategy_docs / operational_context / priorities). Provided via the API.
- The engine also reads the tender's brief + tender_document_content for buyer/operational context.

FEEDBACK STREAMS (graceful, optional)
- AGENT_SYSTEM_PROMPT references BidPricingHistory + BidFeedbackRecord. These tables do not exist yet and are OUT of scope to build. If they are absent, proceed gracefully: mark score_modelling / calibration as "unmodelled — no history yet" and continue. NEVER block a draft for missing history.

HARD CONSTRAINTS (from AGENT_SYSTEM_PROMPT.md — enforce in code, not just prompt)
- No auto-submission. Output is always a DRAFT with status needs_review. No endpoint submits, confirms, or sends anything. Even if asked.
- No invention. Every factual claim carries vault_citations (VaultDocumentVersion IDs). A required slot with no suitable evidence → null + an unfilled_slots entry with a reason. Placeholder text ("TBD") inside content is a failure.
- Tenant isolation. Every vault / brief / content read is org_id (tenant) filtered. A read returning another tenant's data must stop and report.
- Copyright. Paraphrase the ITT; short quoted phrases under 15 words only. Do not reproduce large verbatim ITT chunks in the draft.
- Schema pinning. Draft against templates.yaml schema_version 1.6. Don't invent slots not in the pinned schema.
- Cross-section consistency. Numbers/commitments/KPIs cited in one section must match the same items in other drafted sections of the same tender's package. Check existing drafts before producing new numbers; flag inconsistencies, never silently diverge.

LLM
- Reuse the brief engine's Anthropic client (env ANTHROPIC_API_KEY; model from env, single named constant, default the same model family the brief uses). MUST be injectable/mockable. ALL tests use a fake client returning canned, schema-valid JSON — ZERO real API calls in CI, zero build cost.
- Token budgeting like the brief engine: if the evidence + context exceeds a budget (env constant), prioritise the most relevant vault items + the evaluation criteria; record what was included/truncated/omitted.
- System prompt = the content of AGENT_SYSTEM_PROMPT.md + the specific template's schema injected. Output MUST be the template's response JSON. Validate with pydantic; malformed → retry once stricter; still bad → store nothing, return a clean failure for that question.

STORAGE (new migration)
- submission_packages: id, tender_id FK, org_id, status enum('drafting','needs_review') default 'drafting', created_at, updated_at.
- submission_question_drafts: id, package_id FK, tender_id FK, org_id, template_id, schema_version, question_ref (label/text hash), structured_content jsonb, vault_citations jsonb, confidence_scores jsonb, unfilled_slots jsonb, validation_report jsonb, status enum('needs_review','incomplete') not null, created_at. (incomplete = a blocking validator failed; surfaced, never hidden.)
- All rows org_id stamped. Existing migrations untouched; alembic reaches head.

API (read + draft; NEVER submit)
- POST /tenders/{id}/submission-package/questions  → body: one question's shared_inputs. Drafts it, runs the six checks, stores a submission_question_drafts row (creating the package if needed), returns the draft + validation_report. 402/empty seam noted (see SEAMS) but not enforced this run.
- GET  /tenders/{id}/submission-package  → the package + all its question drafts with statuses.
- GET  /tenders/{id}/submission-package/questions/{draft_id} → one draft.
- 404 unknown tender; 409 brief_not_ready if the tender has no complete brief (the engine reads the brief, it doesn't reproduce it).

SEAMS (clearly marked TODO, do NOT wire this run)
- Payment trigger: generation will sit behind the paid submission-package fee (chunk 6, unmerged). Mark the seam in the POST handler; this run generates without charging.
- Go/no-go gate: per go-no-go.yaml the warnings + client self-certification are shown BEFORE generation. The #71 engine is unmerged. Mark the seam where the gate result will be checked/recorded; do not import #71.
- Self-cert persistence + audit trail: needs the accounts table (chunk 6). Seam only.

RULES
- Build from current main; git pull first; verify dependency symbols (BLOCKED if missing).
- Do NOT change Delta / fetch / brief / payments / cloud behaviour. ADD only.
- No real LLM/network in CI; client mockable; key from env only; never hardcode/log it.
- Human-gated, no-invention, tenant-isolated, copyright-respecting — enforced in code.
- Tests (mocked), 30+ new, all existing green:
  * each of the 5 templates drafts from a fake LLM returning schema-valid JSON → stored draft + validation_report present.
  * no-invention: a required slot with no vault candidate → null + unfilled_slots entry (assert no fabricated content).
  * evidence_minimums / scoring_check / failure_mode checks populate the validation_report; a blocking validator failure → status 'incomplete', surfaced.
  * cross-section consistency: a number that contradicts an earlier drafted section is flagged.
  * tenant isolation: another org's vault item is never offered as a candidate (tested).
  * copyright: draft does not contain a >15-word verbatim run from the provided ITT text (tested on a sample).
