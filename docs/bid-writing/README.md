# Bid Writing Module

Structured templates, drafting agent instructions, and supporting specs for
writing UK public-sector tender responses that consistently score 5/5 on
the standard 0–5 evaluation scale.

## What this module is

A vendor-neutral, sector-neutral specification for how the drafting agent
produces bid responses. Every file here is a build target or a runtime
input for the Phase 5 drafting agent.

This module is the **canonical reference** for both human bid writers
(read the templates, follow the structure, hit the minimums) and the
drafting agent (loads templates.yaml as schema source, AGENT_SYSTEM_PROMPT.md
as system prompt, queries vault feedback streams on every bid).

## Files in this module

| File | Purpose | Loaded by |
|---|---|---|
| `templates.yaml` | Seven response templates (technical, methodology, social value, quality, risk, case study, pricing) plus cross-cutting rules | Drafting agent + human bid writers |
| `AGENT_SYSTEM_PROMPT.md` | Literal system prompt the drafting agent loads on every Claude API call | Drafting agent at runtime |
| `vault-feedback-schemas.md` | Implementation spec for BidPricingHistory and BidFeedbackRecord vault tables | Claude Code in Phase 3 |
| `schedule-schema.yaml` | Canonical structured format for any contract delivery schedule | Drafting agent + Gantt editor + Typst renderer |
| `schedule-implementation-spec.md` | Implementation brief for the schedule subsystem | Claude Code in Phase 5 |
| `CLAUDE_CODE_INSTRUCTIONS.md` | Implementation brief for the drafting module itself | Claude Code in Phase 5 |
| `README.md` | This file | Humans browsing the repo |

## Version pinning

All files in this module reference each other by version. The current set:

- `templates.yaml` schema_version **1.6**
- `AGENT_SYSTEM_PROMPT.md` prompt_version **1.0**, pinned to templates.yaml v1.6 and schedule-schema.yaml v1.0
- `schedule-schema.yaml` schema_version **1.0**

When any of these versions bumps, dependent files must be reviewed for
alignment. The CI pipeline should check that `AGENT_SYSTEM_PROMPT.md`'s
`pinned_templates_yaml_version` matches the actual `templates.yaml`
`schema_version` on every commit.

## The seven templates (in templates.yaml)

| ID | Template | Typical weighting | Notes |
|---|---|---|---|
| 3.1 | technical_capability | 20–40% | Highest-weighted single question on most ITTs |
| 3.2 | methodology_delivery | 10–20% | Plan, mobilisation, resourcing, reporting; draws from schedule-schema |
| 3.3 | social_value | 10%+ mandatory | PPN 002 from Oct 2025; SMART commitments are contractual |
| 3.4 | quality_management | 5–15% | Named QMS, cert numbers, phase-by-phase quality |
| 3.5 | risk_contingency | 5–10% | Contract-specific risks only; generic risks cap at 3 |
| 3.6 | case_study | — | Used standalone or embedded; six-axis match required |
| 3.7 | pricing_schedule | 30–60% | Quality is absolute, price is relative; score modelling from BidPricingHistory |

Each template has five parts:

1. **`inputs`** — what to gather BEFORE writing
2. **`response`** — the slots that make up the answer
3. **`evidence_minimums`** — quantified bar; below these, response cannot 5
4. **`scoring_check`** — fail conditions from qualitative scoring drivers
5. **`failure_modes`** — common ways responses lose marks

## Cross-cutting rules (in templates.yaml)

At the bottom of `templates.yaml`:

- **`vault_feedback_dependencies`** — how the agent consumes BidPricingHistory
  and BidFeedbackRecord on every bid (the learning loop)
- **`visual_identity_isolation`** — tenant-locked brand foundations and unique
  layout fingerprints platform-wide
- **`language_rules`** — banned filler openings, active voice for commitments,
  every "we will" backed by a "we have"
- **`pre_submission_checklist`** — content, compliance, quality, final review

## The drafting agent's runtime flow

When the Phase 5 drafting agent works a question:

```
1. Load AGENT_SYSTEM_PROMPT.md as the system message
2. Identify the question type → select the matching template from templates.yaml
3. Read the template fully
4. Read cross-cutting rules
5. Read the buyer's evaluation criteria for THIS question
6. Read buyer context (strategy docs, NPPS, sector plan, operational context)
7. Query BidPricingHistory and BidFeedbackRecord for the tenant
8. Review vault evidence candidates ranked by relevance
9. Check cross-section state in the bid
10. Draft structured JSON matching the template schema
11. Self-check against evidence_minimums, scoring_check, failure_modes
12. Return draft + validation report + confidence scores
```

The agent never auto-submits. The human approval gate is absolute.

## The schedule subsystem (schedule-schema.yaml + schedule-implementation-spec.md)

The canonical schedule data model feeds three consumers:

1. The `methodology_delivery` template (§3.2) auto-populates from schedule data
2. A web-based Gantt editor for in-platform editing
3. A Typst PDF renderer producing bid-ready Gantt output

No external scheduling tool integration. No MPP/XER/P6/Asta/CSV import or
export. The schedule lives in the platform; the bid pack receives a PDF.

## The vault feedback streams (vault-feedback-schemas.md)

Two tenant-locked tables make the vault a learning system:

- **BidPricingHistory** — every bid's pricing payload, method, outcome,
  competitor data, delivery cost reconciliation. Feeds score-modelling in
  the pricing template after 20–30 bids.
- **BidFeedbackRecord** — debrief contents, section scores, qualitative
  feedback, winning bid intelligence. Calibration signal for the entire
  template system.

Both are tenant-isolated. One tenant's data never feeds another tenant's
drafting.

## How to use (humans)

1. Read the ITT and identify which template fits each question
2. Gather the `inputs` block before drafting
3. Fill each `response` slot, hitting the required fields
4. Self-check against `evidence_minimums` and `scoring_check`
5. Run the cross-cutting `pre_submission_checklist`
6. Get an independent reviewer to red-team it

## How to use (drafting agent — Phase 5)

See `CLAUDE_CODE_INSTRUCTIONS.md` for the implementation brief and
`AGENT_SYSTEM_PROMPT.md` for the runtime instruction set.

## Roadmap

- v1.7+: per-section deep dive analysis files committed alongside the
  templates as `docs/bid-writing/analysis/§3-x-*.md` (optional;
  templates.yaml already encodes the analysis output)
- v2.0: sector-specific overlay files (`templates.construction.yaml`,
  `templates.it_services.yaml`, etc.) that extend the base templates with
  sector-specific evidence requirements, methodology examples, and
  accreditations
- v2.1: per-buyer overlays for repeat customers (NHS, MOD, CCS frameworks)
  once enough bids have flowed through to learn their idiosyncrasies
