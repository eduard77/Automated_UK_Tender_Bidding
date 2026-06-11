# Build progress

This file tracks the Genera Tenders **phased build-and-verify harness** described
in the operator's run prompt. One phase per PR; later phases are gated on the
previous phase being marked DONE.

## Current verdict

| Phase | Title                                                       | Verdict       | Date       | Notes                                                                |
| ----- | ----------------------------------------------------------- | ------------- | ---------- | -------------------------------------------------------------------- |
| 0     | Reusable dashboard bot                                      | **PASS** (CI) | 2026-06-11 | Unit-test gate green; live gate is the operator's runbook below.     |
| 1     | Get the silent sources pulling (Atamis, EU-Supply, Sell2Wales) | not started   | —          | Phase 0 must clear the live gate first.                              |
| 2     | Fix run-15-class discovery errors                           | not started   | —          |                                                                      |
| 3     | Cross-source CPV/field normalisation (the spine)            | not started   | —          | Likely NEEDS DECISION when reached.                                  |
| 4     | Keyword/value/buyer filters as first-class                  | not started   | —          |                                                                      |
| 5     | Per-tender state (interested / not-interested / applied)    | not started   | —          |                                                                      |
| 6     | Guided setup page (word → CPV picker → save)                | not started   | —          | Partly NEEDS DECISION when reached (preset taxonomy).                |
| 7     | Daily digest notifications                                  | not started   | —          |                                                                      |
| 8     | Multi-tenant (accounts, isolation, billing)                 | not started   | —          | NEEDS DECISION when reached (big — return a scoped proposal).        |

## Phase 0 — PASS (CI) (2026-06-11)

Built `tests/e2e/dashboard_bot/` — a Playwright-driven, YAML-spec-driven
dashboard verification bot, plus its own self-tests anchored to the **known
current state** the handover names. The unit-test gate runs in CI and is
green (30 added; full suite 753 passed). The live gate is the operator's
one-command runbook below.

### What was built

```
tender-agent/
├── tests/e2e/
│   ├── dashboard_bot/
│   │   ├── __init__.py        # public package surface
│   │   ├── __main__.py        # `python -m tests.e2e.dashboard_bot`
│   │   ├── README.md          # the package's own runbook
│   │   ├── bot.py             # Playwright driver + pure verdict logic
│   │   ├── cli.py             # argparse entry point
│   │   ├── parser.py          # pure HTML extractors (chips, results, CPV cell)
│   │   ├── report.py          # CheckResult / BotReport + text + JSON renderers
│   │   └── specs.py           # typed CheckSpec / FilterDraft / SearchExpectation / OpenTender
│   ├── fixtures/              # real-shape DOM fixtures
│   │   ├── dashboard_search_5_sources.html
│   │   ├── dashboard_search_empty.html
│   │   ├── dashboard_tender_detail_cf_with_cpv.html
│   │   └── dashboard_tender_detail_proactis_no_cpv.html
│   ├── specs/
│   │   └── phase-0-self.yaml  # 5 Phase-0 self-test specs
│   ├── test_dashboard_bot.py       # 30 unit tests (CI runs these)
│   └── test_dashboard_bot_live.py  # 1 e2e test (skipped unless E2E=1)
```

### Why CI ≠ live gate

The deployed dashboard 403s the project's CI runner range (Azure datacenter
IPs — identical disguise issue to Proactis). The bot is split deliberately:

- **Pure pieces** (`parser.py`, `specs.py`, `report.py`, plus the verdict
  logic inside `bot.py`) are unit-tested against committed real-shape DOM
  fixtures — these run in CI every push and catch parser drift, spec-loader
  bugs, verdict regressions. 30/30 green.
- **Live driver** (`DashboardBot` in `bot.py` + `test_dashboard_bot_live.py`)
  is `@pytest.mark.e2e` and skipped unless `E2E=1`; the operator runs it from
  a residential IP (or the disguised in-process bridge) per the README.

The pure pieces FEED the live driver, so the unit tests and the live run can
never disagree about how the dashboard's DOM is read.

### Phase 0 self-test YAML (`tests/e2e/specs/phase-0-self.yaml`)

Five checks pinning the handover's "known current state":

1. **`source-facet-has-the-five-known-sources`** — Source chips listing CF,
   FTS, PCS, Proactis/ProContract, SAMPLE_SEED (handover §2). Expected: PASS.
2. **`proactis-rows-exist`** — Source=PROACTIS yields ≥1 card (handover §2: 61
   construction tenders inserted). Expected: PASS.
3. **`cpv-filter-returns-nothing-for-proactis-today`** — CPV 45000000 +
   Source=PROACTIS yields 0 cards (handover §3, the headline gap: Proactis
   tenders carry no CPV). Expected: **OBSERVED_GAP** — the bot reports the
   gap truthfully and that's the desired pass for now. Phase 3 flips it
   to PASS.
4. **`silent-sources-absent-from-facet`** — ATAMIS / EU_SUPPLY / S2W must
   NOT appear in result cards on an unfiltered search (handover §7.1).
   Expected: **OBSERVED_GAP**. Phase 1 flips it to PASS.
5. **`cpv-filter-still-returns-cf-and-fts`** — CPV 45000000 + Source=[CF, FTS]
   yields ≥1 card (handover §3: CF/FTS publish CPV directly). Expected: PASS.

### Operator runbook — clear Phase 0's live gate

From a residential IP (or via the disguised bridge):

```bash
cd tender-agent
pip install -e ".[dev]"
playwright install --with-deps chromium     # one-off
python -m tests.e2e.dashboard_bot tests/e2e/specs/phase-0-self.yaml
```

Add `--headed` for a watchable session, `--json` for machine-readable
output. The exit code is `0` only when every spec's observed verdict
matched its expected verdict (PASS and OBSERVED_GAP both count as a
match — that's how the harness records a known gap and proves a later
phase closed it).

If any spec **FAILS**, paste the bot's report; we treat the divergence
as the diagnostic and patch from there (same loop as the Proactis chain).

### Phase 0 verdict

**PASS (CI gate cleared)**: unit tests green; live gate is the operator's
one-command run above. Once the live gate is also clear, Phase 1 begins.

## Next single action

Operator: run the command above against the live dashboard. Once the
report comes back all-pass (or with only OBSERVED_GAPs that match the
YAML), open the next harness run; I'll attempt Phase 1.
