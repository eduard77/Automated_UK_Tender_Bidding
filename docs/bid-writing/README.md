# Bid Writing Templates

Structured templates for writing UK public-sector tender responses that consistently
score 5/5 on the standard 0–5 evaluation scale.

## What this is

`templates.yaml` is the canonical specification for every quality response in a bid.
It exists in two roles:

1. **Reference for human bid writers** — fill the slots, hit the minimums, pass the
   self-check, submit.
2. **Source for the drafting agent (Phase 5)** — the agent loads this YAML, converts
   it to JSON Schema, drives Claude to produce structured drafts that conform, then
   validates against the same rules before letting a human review.

## Why structured

Most bids lose marks for predictable reasons: vague claims, missing evidence,
generic boilerplate, no quantified commitments. A free-form drafting agent makes
those same mistakes because nothing forces it to fill specific evidence slots. By
encoding the slots and the failure modes in YAML, both humans and the agent are
pushed toward the patterns that score 5.

## Files

- `templates.yaml` — the seven question-type templates plus shared inputs and
  cross-cutting rules
- `README.md` — this file
- `CLAUDE_CODE_INSTRUCTIONS.md` — instructions for the Claude Code agent when it
  builds the drafting module (Phase 5)

## The seven templates

| ID | Template | Typical weighting | Notes |
|---|---|---|---|
| 3.1 | technical_capability | 20–40% | Highest-weighted single question on most ITTs |
| 3.2 | methodology_delivery | 10–20% | Plan, mobilisation, resourcing, reporting |
| 3.3 | social_value | 10%+ mandatory | PPN 002 from Oct 2025; SMART commitments are contractual |
| 3.4 | quality_management | 5–15% | Named QMS, cert numbers, phase-by-phase quality |
| 3.5 | risk_contingency | 5–10% | Contract-specific risks only — generic ones cap at 3 |
| 3.6 | case_study | — | Used standalone or embedded; matched on 6 axes |
| 3.7 | pricing_schedule | 30–60% | Quality is absolute, price is relative |

## How each template is structured

Every template has five parts:

1. **`inputs`** — what to gather BEFORE writing. Question text, weighting, word
   limit, evaluation criteria, buyer context. Most templates inherit a shared set.
2. **`response`** — the slots that make up the answer. Each slot has a prompt
   question and required sub-fields. The drafting agent uses these as structured
   output targets.
3. **`evidence_minimums`** — quantified bar. Below these counts, the response
   cannot score 5 by definition. Example: technical_capability requires at least
   1 named client, 1 cert number, 2 named people, 3 numeric KPIs.
4. **`scoring_check`** — fail conditions derived from the qualitative scoring
   drivers. Example: methodology_named fails if the response contains "best
   practice" without naming a specific methodology.
5. **`failure_modes`** — the most common ways responses lose marks. Used as a
   final filter before submission.

## Cross-cutting rules

At the bottom of `templates.yaml`:

- **Language rules** — mirror buyer vocabulary, no filler openings, active voice
  for commitments, every "we will" backed by a "we have"
- **Pre-submission checklist** — content, compliance, quality, final review

These apply to every response regardless of type.

## How to use (humans)

1. Read the ITT and identify which template fits each question.
2. Gather the `inputs` block before drafting.
3. Fill each `response` slot, hitting the required fields.
4. Self-check against `evidence_minimums` and `scoring_check`.
5. Run the cross-cutting `pre_submission_checklist`.
6. Get an independent reviewer to red-team it.

## How to use (drafting agent — Phase 5)

See `CLAUDE_CODE_INSTRUCTIONS.md` for the implementation brief.

In summary: the agent loads `templates.yaml`, generates JSON Schema for each
template, prompts Claude to draft responses that match the schema, validates the
output against `evidence_minimums` and `scoring_check`, and surfaces any failures
to the human reviewer before submission.

## Versioning

`schema_version` at the top of `templates.yaml` tracks the spec. Bumping the
version invalidates any cached drafts produced under the previous version. Every
bid submission records the `schema_version` it was produced under, for audit.

## Roadmap

- v1.0 (current): the seven question types as analysed in §3 of the bid-writing
  template document, with scoring drivers from the qualitative analysis.
- v1.1: section-specific analysis files (per-section deep dives) committed
  alongside the templates as `docs/bid-writing/analysis/§3-x-*.md`.
- v1.2: sector-specific overlays (construction, IT services, professional
  services) that extend the base templates with sector-specific evidence
  requirements.
- v1.3: per-buyer overlays for repeat customers (NHS, MOD, CCS frameworks) once
  enough bids have been observed to learn their idiosyncrasies.
