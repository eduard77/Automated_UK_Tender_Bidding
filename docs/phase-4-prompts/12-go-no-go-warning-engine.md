TASK: Go/No-Go warning engine — turn docs/bid-writing/go-no-go.yaml into a working service (warnings only, never blocks)

GOAL (plain English)
Implement the advisory warning engine specified in docs/bid-writing/go-no-go.yaml. Given a tender that already has a brief, produce: (1) the advisory rating + red warnings, (2) the client self-certification QUESTIONS, and (3) the vault reconciliation warnings (client's word vs the evidence in their vault). It NEVER blocks payment or generation — it only produces warnings and questions. This is self-contained: it depends ONLY on the existing brief + the vault. It must NOT touch payments, accounts, or the cloud database, and must NOT change Delta/fetch/brief behaviour.

READ FIRST
- git pull origin main (phantom-merge guard).
- Read docs/bid-writing/go-no-go.yaml — it is the SPEC. The code implements it. If the file is missing, BLOCKED: commit go-no-go.yaml first.

SOURCE THE DATA (prefer deterministic; no new LLM call needed)
- The tender_briefs row already holds: recommendation, key_risks, contract_value, deadline, mandatory_requirements, scoring, scope_summary. Reuse these — do NOT re-call the LLM to recompute what the brief already has.
- Vault: read the tenant's vault evidence (insurance limits, turnover from accounts, certificate numbers + expiry, accreditations) from the existing vault tables. If the vault tables or a given value aren't present, treat as "unknown" → a please_confirm prompt, NOT an assertion.

PART A — WARNINGS + RATING (from the brief)
- Implement red_warnings exactly as the spec: mandatory_requirement_unmet, cannot_deliver_to_standard, likely_unprofitable, tight_deadline. Derive each from brief fields + vault where possible; where data is missing, emit please_confirm, never a false warning.
- Compute the advisory pillar scores + band (go/conditional/no_go) per the spec's scoring block. This is INFORMATION only. Confidence drops as unknowns rise.
- LLM is OPTIONAL and only for nuanced pillar scoring; if used it MUST be mockable (reuse the brief engine's Anthropic client pattern + env key). Default path needs NO paid call. Tests use a fake client / zero real calls.

PART B — CLIENT SELF-CERTIFICATION QUESTIONS
- Produce the self_certification questions from the spec (has_documents, qualifies, accepts_proceed), with the qualifies question pre-filled with any specific mandatory requirement a red warning flagged.
- Produce the QUESTIONS + the warnings to show alongside them. Do NOT persist the client's answers yet (that needs the accounts table from the gating PR, which is not merged — leave a clearly-marked TODO seam for it). This engine just emits what to ask + what to warn.

PART C — VAULT RECONCILIATION (word vs evidence)
- Implement vault_reconciliation per the spec: for each mandatory_requirement with an extractable value, compare required vs evidenced-in-vault. Emit:
  * contradiction (e.g. certified-qualifies but accounts show £3m vs £5m required) — with requirement, required_value, evidenced_value, source_document
  * expiry (cert/insurance expiring before contract end)
  * shortfall (evidenced value below threshold)
  * please_confirm (no evidence either way — a request, not a warning)
- Re-runnable: callable on demand so it can be invoked whenever the vault changes. A contradiction resolved by later evidence simply stops being emitted.
- Pure data comparison — NO LLM call here.

API
- GET /tenders/{id}/go-no-go → returns the rating + red_warnings + self_certification questions + missing_info, per the spec's output schema.
- GET /tenders/{id}/vault-reconciliation → returns the reconciliation warnings list.
- Both require the tender to have a brief; if none, return a clear "generate a brief first" state.

RULES
- WARNINGS ONLY. The engine never blocks, never refuses, never overrides a client choice. No payment/account/cloud code.
- No invention: every warning grounded in brief or vault data; unknowns → please_confirm, never a fabricated pass/fail.
- Build from current main; existing tests stay green.
- Tests (mocked; zero real LLM/network in CI), 20+ new:
  * red warnings fire correctly from brief fields (unmet mandatory, tight deadline, likely-unprofitable, can't-deliver).
  * unknown data → please_confirm, NOT a warning.
  * reconciliation: £3m-vs-£5m → contradiction with source; expiring cert → expiry; no evidence → please_confirm; later evidence clears it.
  * self-certification questions emitted, qualifies pre-filled with the flagged requirement.
  * advisory band/confidence computed; never returns a block.
  * no-brief tender → clean "generate a brief first" state.
- If blocked, "BLOCKED:" at top with exactly what's needed.

SHIP — OPEN PR, DO NOT MERGE (review after this afternoon's foundation work)
Commits:
- feat(go-no-go): warning engine from go-no-go.yaml spec (rating + red warnings, deterministic)
- feat(go-no-go): client self-certification questions (persistence TODO seam for post-gating)
- feat(go-no-go): vault reconciliation (word vs evidence, re-runnable)
- feat(api): go-no-go + vault-reconciliation endpoints
- test: warnings + reconciliation + self-cert questions + unknown-handling (mocked)
PR title: "feat: Phase 4 — go/no-go warning engine (advisory, warnings-only)"
Description: explain it implements docs/bid-writing/go-no-go.yaml, is warnings-only (never blocks), depends only on brief + vault, defers self-cert persistence to post-gating, and ran with zero paid LLM calls. State at top: "Advisory only — never blocks. Hold merge until foundation work is tested." Sentinel: "Go/no-go warning engine — warnings only, no payment/cloud deps. Ready for review."

Begin.
