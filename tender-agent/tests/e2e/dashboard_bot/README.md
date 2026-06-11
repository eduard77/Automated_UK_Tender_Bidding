# Dashboard Bot — Phase 0 of the build harness

The **reusable dashboard verification bot**. Drives the LIVE Genera
Tenders dashboard via Playwright, evaluates YAML-defined check specs,
emits a readable pass/fail report. Reusable by every later phase.

## Why it exists

The phased build harness needs an automated acceptance check per phase —
"does the dashboard actually do what the phase claims?". Hitting the
backend API would only verify the data store; the bot drives the real
user path (navigate, filter, click, read) so a regression in the
dashboard UI fails the gate too.

## How it's split

| File | What it owns | Pure? |
| --- | --- | --- |
| `specs.py` | `CheckSpec`, `FilterDraft`, `SearchExpectation`, `OpenTender`, YAML loader | yes |
| `parser.py` | Extractors for source chips, result cards, detail CPV cell | yes |
| `report.py` | `CheckResult`, `BotReport`, text + JSON rendering | yes |
| `bot.py` | Live Playwright driver + the verdict logic | no — Playwright at runtime |
| `cli.py` | `python -m tests.e2e.dashboard_bot` entry point | no |

The PURE pieces are unit-tested in `tests/e2e/test_dashboard_bot.py` and
run with the rest of the backend suite in CI. The LIVE bot is exercised
on demand by the operator (see below).

## Running the bot

```bash
# Headless against the deployed dashboard (operator's residential IP):
python -m tests.e2e.dashboard_bot tests/e2e/specs/phase-0-self.yaml

# Watchable session (operator):
python -m tests.e2e.dashboard_bot --headed tests/e2e/specs/phase-0-self.yaml

# Machine-readable report for log capture:
python -m tests.e2e.dashboard_bot --json tests/e2e/specs/phase-0-self.yaml

# Different URL (preview slot, staging, local dev):
python -m tests.e2e.dashboard_bot --url http://localhost:3000 tests/e2e/specs/phase-0-self.yaml
```

The exit code is `0` only when every spec's observed verdict matched its
expected verdict. `PASS` and `OBSERVED_GAP` both count as a match — the
latter is how Phase 0 records a known gap (e.g. "Proactis carries no CPV
today"); flipping the YAML's `expected_outcome` from `OBSERVED_GAP` to
`PASS` after a later phase fixes it is the regression proof.

## Where to run it from (important)

The deployed dashboard 403s the **Azure datacenter range** — probed today
from this environment. CI runs on `ubuntu-latest` GitHub Actions runners
which sit in a comparable range, so we **deliberately do NOT** wire the
live bot into CI. Instead:

- **CI**: runs the pure unit tests under `tests/e2e/test_dashboard_bot.py`
  every push. Catches a parser drift, a spec-loader bug, a verdict-logic
  regression — without touching the live URL.
- **Operator** (residential IP, or via the disguised in-process bridge):
  runs `python -m tests.e2e.dashboard_bot tests/e2e/specs/phase-N.yaml`
  whenever a phase asks for a live gate.

The bot reuses the proven cloud-browser disguise from
`tender_agent.services.bridge_in_process` (real Chromium UA, viewport,
locale, navigator.webdriver hide) so the same shape that broke Proactis
open carries straight over. If the disguise import fails the bot falls
back to plain Chromium — fine for local-dev runs against
`http://localhost:3000`.

## Adding checks for a later phase

1. Drop a YAML at `tests/e2e/specs/phase-N.yaml`. Each entry is a
   `CheckSpec` (see `specs.py` for the typed shape).
2. If the spec needs a new assertion (e.g. "tender detail shows a
   non-empty CPV row"), extend `SearchExpectation` / `OpenTender` and
   add a matching branch in `bot._evaluate`.
3. Add a unit test against a saved DOM fixture if a new selector is in
   play. The fixtures live in `tests/e2e/fixtures/`.

The bot is intentionally narrow — it's not meant to be a full test
framework. It's the gate.
