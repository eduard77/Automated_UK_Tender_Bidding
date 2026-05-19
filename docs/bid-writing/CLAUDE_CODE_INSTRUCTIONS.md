# Claude Code Instructions — Bid Drafting Module

**Phase**: 5 (Drafting Agent)
**Prerequisites**: Phase 1–4 complete; vault populated; tender requirements
extractor producing structured `tender_requirements` records.
**Inputs**: `docs/bid-writing/templates.yaml`, `docs/bid-writing/README.md`

These are the instructions for the Claude Code agent when we reach Phase 5. Paste
this as the task brief, or reference it from a GitHub issue with `@claude`.

---

## Context

We have a YAML specification at `docs/bid-writing/templates.yaml` describing seven
templates for the question types found in UK public-sector tender responses. Each
template defines:

- The inputs to gather before drafting
- The response slots with required fields
- Quantified evidence minimums
- Scoring self-check fail conditions
- Failure modes to reject before submission

This module turns that spec into an operational drafting service. It is the heart
of Phase 5.

## What you are building

A Python module `tender_agent.services.drafting` that:

1. Loads `docs/bid-writing/templates.yaml` and converts each template into a
   Pydantic model + JSON Schema.
2. Exposes a `draft_response(tender_id, question_id, template_id)` function that:
   a. Pulls the tender's `TenderRequirements` record for the question.
   b. Gathers buyer context from the vault (their published strategy docs, prior
      contracts, sector priorities).
   c. Queries the vault for evidence candidates (case studies, certs, named team
      members, past responses) ranked by relevance to this question.
   d. Calls Claude (Sonnet) with the template's prompts as structured-output
      targets, the vault evidence as supporting context, and strict instructions
      to cite vault items by `VaultDocumentVersion.id` for every claim.
   e. Validates the structured output against `evidence_minimums` and
      `scoring_check` fail conditions. Any failure → flag for review with the
      specific rule that failed.
   f. Stores the draft as `BidResponse(question_id, template_id,
      schema_version, structured_content, validation_results, vault_citations,
      confidence_scores)` ready for human review.
3. Exposes a `validate_response(bid_response_id)` function that re-runs all
   checks (useful after human edits).
4. Exposes a `render_response(bid_response_id, format)` function that converts
   the structured content into the buyer's required output format (text, Word,
   PDF) using the buyer's template if mandated.

## File layout

```
tender-agent/
  src/tender_agent/
    services/
      drafting/
        __init__.py
        loader.py           # loads templates.yaml, builds Pydantic models
        evidence.py         # vault retrieval ranked by relevance
        prompts.py          # Claude prompt construction per template
        validators.py       # evidence_minimums + scoring_check enforcement
        renderer.py         # structured → text/docx/pdf
        service.py          # the public draft_response / validate / render
    models.py               # add BidResponse, BidResponseValidation
  tests/
    services/drafting/
      test_loader.py
      test_evidence.py
      test_validators.py
      test_service.py
      fixtures/
        templates_minimal.yaml  # for fast tests
        tender_fixture.json
        vault_fixture.json
  docs/bid-writing/
    templates.yaml          # already exists; do not modify in this task
    README.md               # already exists
    CLAUDE_CODE_INSTRUCTIONS.md   # this file
```

## Implementation requirements

### loader.py

- Parse `templates.yaml` with `pyyaml`, resolve `inherits: shared_inputs`.
- For each template, generate a Pydantic `BaseModel` whose fields match the
  `response` slots and whose validators enforce the `required` constraints
  (min_items, item_fields, allowed values, max_words).
- Cache parsed models keyed by `(template_id, schema_version)`.
- Public functions:
  - `load_templates() -> dict[str, TemplateSpec]`
  - `get_pydantic_model(template_id: str) -> Type[BaseModel]`
  - `get_json_schema(template_id: str) -> dict`

### evidence.py

- Queries `VaultDocumentVersion` with semantic + structured filters.
- Ranks evidence per question by: sector match, value proximity (±50% for case
  studies), recency, criterion match.
- Returns up to N candidates per slot type (case_studies, certs, named_people,
  past_responses, kpis_achieved).
- Public function:
  - `get_evidence_candidates(tender_id, question_id, template_id) -> EvidenceBundle`

### prompts.py

- Builds the Claude prompt for a given template + evidence bundle.
- The system prompt is generic ("You are a UK public-sector bid writer. You
  produce structured JSON matching the supplied schema. You cite every claim by
  vault_id. You never invent facts.").
- The user prompt:
  - States the question text and evaluation criteria verbatim.
  - Provides the buyer context block.
  - Provides the evidence candidates as a structured list with vault_ids.
  - Provides the template's response slots as the JSON output target.
  - Reminds Claude of the cross_cutting_rules (mirror vocabulary, no filler,
    active voice, every "we will" backed by "we have").
- Strict rule for Claude in the prompt: "If a required slot cannot be filled
  from the provided evidence, output `null` and add an entry to the
  `unfilled_slots` array with a `reason`. Do not invent."

### validators.py

- For a draft, runs:
  - Pydantic schema validation (structural)
  - `evidence_minimums` check (count named clients, cert numbers, etc.)
  - Each `scoring_check` fail condition as a function
  - `failure_modes` heuristic pass (string-matching the banned patterns)
- Returns `ValidationResult(passed: bool, failures: list[Failure])` where each
  failure names the rule and points to the offending slot.

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
    bid_id: UUID                     # FK to Bid (Phase 4)
    question_id: UUID                # FK to TenderRequirements question
    template_id: str                 # one of the seven IDs from templates.yaml
    schema_version: str              # captured from templates.yaml at draft time
    structured_content: dict         # the JSON output from Claude
    vault_citations: list[UUID]      # VaultDocumentVersion.id list
    confidence_scores: dict          # per slot
    validation_results: dict         # latest ValidationResult serialised
    status: enum                     # draft / human_reviewing / approved / rejected
    drafted_at: datetime
    approved_at: datetime | None
    approved_by: str | None
```

## Hard rules (do not violate)

1. **No claim without a vault citation.** Every factual assertion in the draft
   must be backed by a `VaultDocumentVersion.id`. The Pydantic models enforce
   this with a per-claim `vault_id` field.
2. **No hallucinated certs, dates, values, or names.** If the vault doesn't have
   evidence for a slot, the agent outputs `null` and flags for review. It does
   not invent.
3. **Schema version pinning.** Every `BidResponse` records the
   `schema_version` it was drafted under. If `templates.yaml` is bumped, old
   drafts remain valid for their original version but cannot be edited under
   the new schema without re-validation.
4. **Human approval gate.** Drafts are never auto-submitted. The status
   transitions are: `draft` → `human_reviewing` → (`approved` | `rejected`).
   Only `approved` drafts can be included in a `Submission`.
5. **Audit log.** Every draft/validation/render/edit is logged with timestamp,
   actor, input hash, output hash, and outcome. Use the existing audit log
   infrastructure from Phase 4.
6. **No business logic in API handlers.** All drafting logic lives in the
   `services/drafting/` module. The API route is a thin wrapper.

## Testing

- Unit tests for `loader.py` covering all seven templates and the shared inputs
  inheritance.
- Unit tests for `validators.py` covering each `scoring_check` fail condition
  with positive and negative cases.
- Integration test: fixture tender + fixture vault → full `draft_response` call
  → assert structured output validates → assert vault citations resolve →
  assert all evidence_minimums hit.
- Snapshot test for one rendered output per format (text, docx, pdf) using a
  golden file.
- No live Claude calls in CI; use a recorded fixture response from a previous
  Claude call (HAR-style) for the drafting test.

## Acceptance criteria

The PR is accepted when:

- [ ] All seven templates load and produce valid Pydantic models.
- [ ] `draft_response` produces a `BidResponse` with all required slots filled
      or explicitly flagged as `unfilled_slots`.
- [ ] Every slot in a successful draft has a resolvable `vault_id` citation.
- [ ] Every `scoring_check` fail condition has a unit test (positive + negative).
- [ ] `validate_response` after a human edit catches a deliberately-broken
      draft (test mutates the structured content to violate a rule).
- [ ] `render_response` produces a text, docx, and pdf for the same draft and
      they match a golden fixture (allowing for timestamp diffs).
- [ ] Audit log entries written for draft / validate / render / approve.
- [ ] Lint clean (ruff), type-clean (mypy strict on this module), 90%+ test
      coverage on the module.

## Out of scope (do NOT do)

- Do not modify `docs/bid-writing/templates.yaml` in this task. If the spec
  needs changes, raise it as a separate issue.
- Do not implement portal submission. That is Phase 4/6.
- Do not implement the case study generator (Phase 5.6) — it has a separate
  brief.
- Do not change the vault schema; use what Phase 3 provides.
- Do not add a UI in this task. Dashboard pages for the bid editor come in a
  follow-up task referencing this module.

## When you finish

Open a PR titled **"Phase 5: Bid drafting module using templates.yaml"**. In
the PR description include:

1. Test coverage report.
2. One end-to-end example: a real ITT question from the fixture set, the
   evidence the agent gathered, the structured draft produced, the validation
   result, and the rendered text output.
3. Any deviations from this brief, with justification.
4. Updated PROJECT.md noting Phase 5 status.

Pause for human review of the PR before merging. Do not self-merge.
