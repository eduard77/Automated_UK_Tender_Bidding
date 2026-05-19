# Claude Code Instructions — Bid Drafting Module

**Phase**: 5 (Drafting Agent)
**Prerequisites**:
- Phase 1–4 complete
- Phase 3 vault populated, including BidPricingHistory and BidFeedbackRecord
  tables per `vault-feedback-schemas.md`
- Tender requirements extractor producing structured `tender_requirements` records
- Tenant brand foundation onboarded (required for case study and schedule
  PDF render)

**Inputs**:
- `docs/bid-writing/templates.yaml` (schema_version 1.6)
- `docs/bid-writing/AGENT_SYSTEM_PROMPT.md` (prompt_version 1.0)
- `docs/bid-writing/schedule-schema.yaml` (schema_version 1.0)
- `docs/bid-writing/vault-feedback-schemas.md`
- `docs/bid-writing/README.md`

These are the instructions for the Claude Code agent when we reach Phase 5.
Paste this as the task brief, or reference it from a GitHub issue with
`@claude`.

---

## Context

The bid drafting module produces structured responses to UK public-sector
tender questions that consistently score 5/5 on the standard 0–5 evaluation
scale. The module is fully specified across the files listed above:

- `templates.yaml` defines seven response templates (technical_capability,
  methodology_delivery, social_value, quality_management, risk_contingency,
  case_study, pricing_schedule) plus cross-cutting rules.
- `AGENT_SYSTEM_PROMPT.md` is the literal system prompt the drafting agent
  loads on every Claude API call.
- `vault-feedback-schemas.md` specifies the two tenant-locked tables
  (BidPricingHistory, BidFeedbackRecord) the agent must query on every bid.
- `schedule-schema.yaml` specifies the canonical schedule format that feeds
  the methodology_delivery template and the Gantt subsystem.

This module turns those specs into an operational drafting service. It is
the heart of Phase 5.

## What you are building

A Python module `tender_agent.services.drafting` that:

1. **Loads and validates the specifications** —
   `docs/bid-writing/templates.yaml` and `docs/bid-writing/schedule-schema.yaml`
   loaded and converted into Pydantic models + JSON Schemas. Caches keyed by
   `(template_id, schema_version)`.

2. **Loads the system prompt** —
   `docs/bid-writing/AGENT_SYSTEM_PROMPT.md` loaded as the literal system
   message on every Claude API call. The CI pipeline checks that the prompt's
   `pinned_templates_yaml_version` matches the loaded templates' `schema_version`.

3. **Exposes a `draft_response(tender_id, question_id, template_id)` function** that:

   a. Pulls the tender's `TenderRequirements` record for the question.

   b. Gathers buyer context from the vault (their published strategy docs,
      prior contracts, sector priorities, operational context).

   c. **Queries the feedback streams** per
      `templates.yaml.cross_cutting_rules.vault_feedback_dependencies`:
      - For pricing drafts: `BidPricingHistory` for comparable past bids
        (same sector, value within ±50%, similar buyer, last 3 years).
        Surface comparable price points, winning prices, delivered cost.
      - For quality drafts: `BidFeedbackRecord` for past feedback on the
        same template_id. Surface specific gaps from past low-scoring
        sections. Flag any recurring low-scoring patterns.

   d. **Queries the vault for evidence candidates** ranked by relevance to
      this question. Filter out expired certs and case studies below the
      six-axis match threshold. Check client consent before including
      named clients.

   e. **Checks cross-section state**: which KPIs, commitments, risks have
      already been drafted in this bid. Ensures consistency.

   f. **Calls Claude (Sonnet)** with:
      - System message: `AGENT_SYSTEM_PROMPT.md` verbatim
      - User message: template schema as output target, question text,
        evaluation criteria, buyer context, feedback history, evidence
        candidates, cross-section state

   g. **Validates the structured output** against:
      - Pydantic schema (structural)
      - `evidence_minimums` (count thresholds)
      - `scoring_check` (each fail_if condition, with blocking severity
        respected)
      - `failure_modes` (pattern checks)
      - Cross-section consistency

   h. **Stores the draft** as `BidResponse(question_id, template_id,
      schema_version, structured_content, validation_results,
      vault_citations, confidence_scores, feedback_consumed)` ready for
      human review.

4. **Exposes a `validate_response(bid_response_id)` function** that re-runs
   all checks (useful after human edits).

5. **Exposes a `render_response(bid_response_id, format)` function** that
   converts the structured content into the buyer's required output format
   (text, Word, PDF) using the buyer's template if mandated.

## File layout

```
tender-agent/
  src/tender_agent/
    services/
      drafting/
        __init__.py
        loader.py               # loads templates.yaml + schedule-schema.yaml
        system_prompt.py        # loads AGENT_SYSTEM_PROMPT.md, version check
        evidence.py             # vault retrieval ranked by relevance
        feedback.py             # BidPricingHistory + BidFeedbackRecord queries
        cross_section.py        # cross-section state tracking and validation
        prompts.py              # Claude prompt construction per template
        validators.py           # evidence_minimums + scoring_check enforcement
        renderer.py             # structured → text/docx/pdf
        service.py              # public draft_response / validate / render
    models.py                   # add BidResponse, BidResponseValidation
  tests/
    services/drafting/
      test_loader.py
      test_system_prompt.py
      test_evidence.py
      test_feedback.py
      test_cross_section.py
      test_validators.py
      test_service.py
      fixtures/
        templates_minimal.yaml
        tender_fixture.json
        vault_fixture.json
        bid_pricing_history_fixture.json
        bid_feedback_record_fixture.json
  docs/bid-writing/
    templates.yaml              # already exists; do not modify in this task
    AGENT_SYSTEM_PROMPT.md      # already exists; do not modify in this task
    schedule-schema.yaml        # already exists; do not modify in this task
    vault-feedback-schemas.md   # already exists; do not modify in this task
    README.md                   # already exists
    CLAUDE_CODE_INSTRUCTIONS.md # this file
```

## Implementation requirements

### loader.py

- Parse `templates.yaml` and `schedule-schema.yaml` with `pyyaml`, resolve
  `inherits: shared_inputs`.
- For each template, generate a Pydantic `BaseModel` whose fields match the
  `response` slots and whose validators enforce `required` constraints
  (min_items, item_fields, allowed values, max_words).
- Cache parsed models keyed by `(template_id, schema_version)`.
- Public functions:
  - `load_templates() -> dict[str, TemplateSpec]`
  - `load_schedule_schema() -> ScheduleSpec`
  - `get_pydantic_model(template_id: str) -> Type[BaseModel]`
  - `get_json_schema(template_id: str) -> dict`

### system_prompt.py

- Load `AGENT_SYSTEM_PROMPT.md` from `docs/bid-writing/`.
- Parse the YAML metadata block at the top to extract `prompt_version`,
  `pinned_templates_yaml_version`, `pinned_schedule_schema_version`.
- **Version-pinning check**: verify the loaded `templates.yaml` `schema_version`
  matches the prompt's `pinned_templates_yaml_version`. If they differ,
  raise a fatal error at startup. This prevents drift between the prompt
  and the templates it references.
- Public function: `load_system_prompt() -> str` returning the full markdown
  content as a string, suitable for direct use as a Claude system message.

### evidence.py

- Queries `VaultDocumentVersion` with semantic + structured filters.
- Ranks evidence per question by:
  - For case studies: six-axis match (sector / value ±50% / complexity /
    geography / technology stack / recency <3 years); reject if
    `overall_relevance_score < 3.0`
  - For certificates: expiry check; reject if expired or expiring within
    60 days
  - For client-naming content: check `client_consent_register`; respect
    use_named/use_logo/use_staff_photos flags
- Returns up to N candidates per slot type (case_studies, certs,
  named_people, past_responses, kpis_achieved).
- Public function:
  `get_evidence_candidates(tender_id, question_id, template_id) -> EvidenceBundle`

### feedback.py

- Queries `BidPricingHistory` and `BidFeedbackRecord` per the SQL examples
  in `vault-feedback-schemas.md`.
- **Tenant isolation enforced**: every query includes `tenant_id` filter;
  Row-Level Security at DB level provides defence-in-depth.
- Public functions:
  - `get_comparable_pricing_history(tenant_id, sector, value_gbp,
    buyer_id) -> list[BidPricingRecord]`
  - `get_buyer_pricing_method_pattern(tenant_id, buyer_id) -> dict`
  - `get_feedback_for_template(tenant_id, template_id, buyer_id=None,
    sector=None) -> list[FeedbackRecord]`
  - `get_recurring_low_scoring_patterns(tenant_id, template_id) -> list[dict]`

### cross_section.py

- Tracks state across drafts within a single bid:
  - KPIs cited per section
  - Commitments made per section
  - Risks referenced per section
  - Schedule milestones used per section
- Provides validation: before a new draft is committed, check that any
  numbers/entities it references match what other sections have already
  used.
- Public function:
  `validate_cross_section_consistency(bid_id, new_draft) -> CrossSectionValidationResult`

### prompts.py

- Builds the Claude user prompt for a given template + evidence bundle +
  feedback bundle + cross-section state. The system prompt comes from
  `system_prompt.py`; this module composes the user message.
- The user prompt:
  - States the question text and evaluation criteria verbatim
  - Provides the buyer context block
  - Provides the BidPricingHistory and BidFeedbackRecord summaries
  - Provides the evidence candidates as a structured list with vault_ids
  - Provides the template's response slots as the JSON output target
  - Provides the cross-section state (what's already been drafted in this
    bid)
  - References the system prompt's hard rules (does NOT repeat them; the
    system prompt has them already)

### validators.py

- For a draft, runs:
  - Pydantic schema validation (structural)
  - `evidence_minimums` check (count named clients, cert numbers, etc.)
  - Each `scoring_check` fail condition as a function
  - `failure_modes` heuristic pass (string-matching the banned patterns)
  - `cross_section.validate_cross_section_consistency`
- Returns `ValidationResult(passed: bool, failures: list[Failure])` where
  each failure names the rule, severity (blocking/non-blocking), and
  points to the offending slot.
- **Blocking failures prevent the draft from being committed.** The agent
  must either fix the draft or return it with status `incomplete` and
  the specific blocking failure surfaced for human resolution.

### service.py

The public surface:

```python
def draft_response(
    db: Session,
    tender_id: UUID,
    question_id: UUID,
    template_id: str,
    force_redraft: bool = False,
) -> BidResponse:
    """Produce a structured draft response for one question."""

def validate_response(
    db: Session,
    bid_response_id: UUID,
) -> ValidationResult:
    """Re-run validation against current schema (use after human edits)."""

def render_response(
    db: Session,
    bid_response_id: UUID,
    output_format: Literal["text", "docx", "pdf"],
    buyer_template_path: Path | None = None,
) -> Path:
    """Render structured content into the buyer's required output format."""
```

### models.py — additions

```python
class BidResponse(Base):
    id: UUID
    tenant_id: UUID                      # tenant-locked, RLS enforced
    bid_id: UUID                         # FK to Bid (Phase 4)
    question_id: UUID                    # FK to TenderRequirements question
    template_id: str                     # one of the seven IDs from templates.yaml
    schema_version: str                  # captured from templates.yaml at draft time
    prompt_version: str                  # captured from AGENT_SYSTEM_PROMPT.md at draft time
    structured_content: dict             # the JSON output from Claude
    vault_citations: list[UUID]          # VaultDocumentVersion.id list
    confidence_scores: dict              # per slot
    feedback_consumed: dict              # which BidPricingHistory/BidFeedbackRecord IDs informed the draft
    cross_section_alignments: dict       # KPIs, commitments, risks aligned with other sections
    validation_results: dict             # latest ValidationResult serialised
    status: enum                         # draft / incomplete / human_reviewing / approved / rejected
    drafted_at: datetime
    approved_at: datetime | None
    approved_by: str | None
```

## Hard rules (per AGENT_SYSTEM_PROMPT.md — do not violate)

These are duplicated here for emphasis. The system prompt enforces them at
runtime; this module enforces them at build time.

1. **Tenant isolation**: every query includes tenant_id; RLS enforces at DB
   level. Cross-tenant leak tests pass.
2. **No claim without a vault citation**: every factual assertion in the
   draft must be backed by a `VaultDocumentVersion.id`.
3. **No hallucinated certs, dates, values, or names**: if the vault doesn't
   have evidence for a slot, the agent outputs `null` and flags for review.
4. **Schema version pinning**: every `BidResponse` records the
   `schema_version` and `prompt_version` it was drafted under.
5. **Human approval gate**: drafts never auto-submitted. Status transitions
   `draft` → `human_reviewing` → (`approved` | `rejected`). Only `approved`
   drafts can be included in a `Submission`.
6. **Blocking validators block commit**: drafts that fail any blocking
   validator cannot transition to `human_reviewing` until the failure is
   resolved or the draft is explicitly marked `incomplete`.
7. **Cross-section consistency**: numbers cited in one section must match
   the same numbers cited in other sections of the same bid.
8. **Audit log**: every draft/validation/render/edit logged with timestamp,
   actor, input hash, output hash, outcome.
9. **No business logic in API handlers**: all drafting logic in
   `services/drafting/`. API route is a thin wrapper.

## Testing

- Unit tests for `loader.py` covering all seven templates and shared inputs
  inheritance.
- Unit tests for `system_prompt.py` covering version pinning check (positive
  and negative cases).
- Unit tests for `validators.py` covering each `scoring_check` fail
  condition with positive and negative cases. Every blocking validator has
  an explicit test that confirms it blocks.
- Unit tests for `feedback.py` covering each query type with tenant
  isolation verification.
- Unit tests for `cross_section.py` covering KPI/commitment/risk alignment
  detection.
- Integration test: fixture tender + fixture vault + fixture
  BidPricingHistory + fixture BidFeedbackRecord → full `draft_response` call
  → assert structured output validates → assert vault citations resolve →
  assert all evidence_minimums hit → assert feedback consumed correctly.
- Snapshot test for one rendered output per format (text, docx, pdf) using
  a golden file.
- **Cross-tenant leak test**: verify that a draft_response call for tenant
  A's bid cannot return content citing tenant B's vault items, pricing
  history, or feedback records.
- No live Claude calls in CI; use a recorded fixture response from a
  previous Claude call (HAR-style) for the drafting test.

## Acceptance criteria

The PR is accepted when:

- [ ] All seven templates load and produce valid Pydantic models.
- [ ] Schedule schema loads and produces valid Pydantic models.
- [ ] System prompt loads with successful version-pinning check.
- [ ] `draft_response` produces a `BidResponse` with all required slots
      filled or explicitly flagged as `unfilled_slots`.
- [ ] Every slot in a successful draft has a resolvable `vault_id`
      citation.
- [ ] BidPricingHistory and BidFeedbackRecord queries verified to surface
      relevant historical data to the agent.
- [ ] Every `scoring_check` fail condition has a unit test (positive +
      negative). Every blocking validator has an explicit block test.
- [ ] `validate_response` after a human edit catches a deliberately-broken
      draft (test mutates the structured content to violate a rule).
- [ ] `render_response` produces text, docx, and pdf for the same draft;
      matches golden fixture (allowing for timestamp diffs).
- [ ] Cross-tenant leak test passes.
- [ ] Audit log entries written for draft / validate / render / approve.
- [ ] Lint clean (ruff), type-clean (mypy strict on this module), 90%+
      test coverage on the module.

## Out of scope (do NOT do)

- Do not modify any file under `docs/bid-writing/` in this task. If specs
  need changes, raise as separate issues.
- Do not implement portal submission. That is Phase 4/6.
- Do not implement the case study generator (separate brief).
- Do not implement the schedule subsystem (separate brief —
  `schedule-implementation-spec.md`).
- Do not change the vault schema; use what Phase 3 provides.
- Do not add a UI in this task. Dashboard pages for the bid editor come in
  a follow-up task referencing this module.

## When you finish

Open a PR titled **"Phase 5: Bid drafting module using templates.yaml v1.6"**.
In the PR description include:

1. Test coverage report.
2. One end-to-end example: a real ITT question from the fixture set, the
   evidence the agent gathered, the feedback history surfaced, the
   structured draft produced, the validation result, and the rendered
   text output.
3. Any deviations from this brief, with justification.
4. Updated PROJECT.md noting Phase 5 status.

Pause for human review of the PR before merging. Do not self-merge.
