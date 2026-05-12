# Extractor validation

The requirements extractor (`services/requirements_extractor.py`) calls Claude
over a tender's documents and persists a structured brief. Phase 2's
acceptance criterion is "extractor validated on ≥10 real tenders, no
hallucinated fields." This document covers how to run that validation, how
to read the report, and when to retune the prompt.

## What gets checked

For each tender the script validates three things:

### 1. Schema conformance

The extractor's JSON must match the contract in `services/requirements_extractor.py`
(documented also in PROJECT.md §5.3):

- All ten top-level keys present (`summary`, `evaluation_criteria`,
  `mandatory_requirements`, `desired_requirements`, `documents_required`,
  `questions_to_answer`, `risk_flags`, `estimated_effort_days`,
  `recommendation`, `recommendation_reason`).
- No unexpected top-level keys (the script tolerates `model`, `extracted_at`,
  `raw_response` which come from the DB row, not the prompt schema).
- `recommendation ∈ {"pursue", "decline", "review"}`.
- Each `mandatory_requirements[].confidence`, when present, `∈ {"high", "medium", "low"}`.
- Every list-typed field is actually a list (catches the model returning
  `null` instead of `[]`).

### 2. Requirement grounding

For each `mandatory_requirements[].requirement` string the script computes
case-folded token overlap (≥3 chars) with the tender's source text (description
plus every downloaded document). The requirement is considered grounded if:

- **token overlap ≥ 30%**, _or_
- **`confidence == "low"`** — Claude explicitly flagged the requirement as a guess.

Anything failing both is reported as "potentially hallucinated requirement"
with the overlap percentage and the requirement text.

### 3. Number grounding

Every numeric literal that appears anywhere in the extracted output (amounts,
percentages, year dates, counts) must appear verbatim in the source text.
A number in the output that's not in the source is flagged as "potentially
hallucinated number" with the JSON path where it was found.

Heuristic carve-outs: numbers under these keys are ignored, because they're
operator-side judgements or synthetic identifiers, not claims about source:
`id`, `estimated_effort_days`, `weight`, `weight_pct`, `word_limit`.

## Running the script

```bash
cd tender-agent
. .venv/bin/activate  # or your usual venv
export DATABASE_URL=postgresql+psycopg://tender:tender@localhost:5432/tender_agent
export ANTHROPIC_API_KEY=sk-ant-...

# One specific tender
python scripts/validate_extractor.py --tender-id 42 --output report.md

# The 20 most recent tenders that don't yet have a brief
python scripts/validate_extractor.py --recent 20 --output report.md

# Everything without a brief — useful for a one-off catch-up sweep
python scripts/validate_extractor.py --all-without-brief --output report.md

# Schema-only dry run (no Anthropic call, no credits)
python scripts/validate_extractor.py --recent 5 --dry-run
```

Exit codes:
- `0` — all processed tenders passed (schema clean, < 2% hallucination).
- `1` — at least one tender failed (schema errors or ≥ 2% hallucination).
- `2` — no tenders matched the selection criteria.

The script writes a markdown report to `--output` or stdout. Selecting a CI
gate against exit code = 0 is reasonable once a sufficient corpus exists.

## Target threshold

**Phase 2 acceptance:** run the script across ≥ 10 real tenders. Pass if:

- 100% schema conformance, and
- < 2% of total claims (requirements + numbers) flagged as potentially
  hallucinated.

A `<2%` rate on a typical UK ITT means roughly 0-1 false flags per 50 claims.
Above that, retune the prompt — see "When to retune" below.

## When to retune the prompt

A failed run typically points at one of three causes:

1. **Genuine hallucination** — Claude invented a figure or requirement.
   Update `SYSTEM_PROMPT` in `services/requirements_extractor.py` to tighten
   the "never invent" instruction; consider adding an explicit example of the
   class of fabrication that occurred. Re-run on the same tender corpus.

2. **Source-text gap** — the heuristic flagged a number that *was* in the
   document, but document text wasn't aggregated correctly (PDF parser missed
   a column, DOCX table cells split across lines, etc.). Fix the document
   downloader / aggregator, not the extractor.

3. **Heuristic noise** — the flag is technically correct but operationally
   uninteresting (e.g. "100" appeared in a list of 100 enumeration but not in
   the source as a literal "100"). Tighten the carve-outs in
   `scripts/validate_extractor.py::_GROUNDING_SKIP_KEYS` or the token regex.
   Annotate the PR with the specific class of noise observed.

If real tenders surface a class of hallucination not covered by these three
heuristics (e.g. invented buyer names, fabricated CPV codes, made-up dates
that *do* appear in the source but in an unrelated context), open an issue
with the report excerpt, and add a new check function in
`validate_extractor.py` alongside `check_schema` /
`check_requirement_grounding` / `check_number_grounding`.

## CI integration (future)

Currently the script is operator-run. Once Phase 6 lands the document
re-download workflow, this script becomes a nightly job:

- Pick the previous day's matched tenders that have a brief.
- Run validation on each.
- Post a markdown summary to the team channel.
- Open a tracking issue if `hallucination_rate >= 2%` for any tender.

Until then, run it manually after each prompt change and after each notable
batch of new tenders.

## Triggering one-off extraction

The dashboard's "Generate brief" button (T2.3) and the validation script both
go through the same admin endpoint:

```bash
curl -X POST http://localhost:8000/admin/extract-requirements/42
```

Returns `200` with the new `TenderRequirementsRead`, or:

- `404` if the tender doesn't exist.
- `422` if the tender has no description AND no downloaded documents.
- `503` if `ANTHROPIC_API_KEY` is unset.
- `502` if Claude returned an error or unparseable JSON. Check structured
  logs for `requirements.api_error` / `requirements.parse_failed`.
