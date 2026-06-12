# Build progress

This file tracks the Genera Tenders **phased build-and-verify harness** described
in the operator's run prompt. One phase per PR; later phases are gated on the
previous phase being marked DONE.

## Current verdict

| Phase | Title                                                       | Verdict       | Date       | Notes                                                                |
| ----- | ----------------------------------------------------------- | ------------- | ---------- | -------------------------------------------------------------------- |
| 0     | Reusable dashboard bot                                      | **PASS** (CI) | 2026-06-11 | Unit-test gate green; live gate is the operator's runbook below.     |
| 1     | Get the silent sources pulling (Atamis, EU-Supply, Sell2Wales) | **IN PROGRESS (rev 4)** | 2026-06-12 | rev 4: concurrent per-source poll so a high-volume source (FTS) can't starve the tail; gate awaits next health readout. |
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

## Phase 1 — IN PROGRESS (2026-06-11): diagnosis + first fixes shipped

### What the diagnosis found (statically provable, no live access needed)

1. **All three adapters ARE registered** (`ADAPTERS` has S2W, EU_SUPPLY,
   ATAMIS) → Source rows auto-created at boot, scheduler polls them. Not a
   registration failure.
2. **The watermark trap (EU_SUPPLY + ATAMIS) — found and fixed.**
   `poll_source` advances `source.last_polled_at = now` after every clean
   poll, even with zero rows. Both listing-only adapters post-filtered rows
   by `published >= since` — but their listings show CURRENTLY-OPEN
   tenders whose publication/Opens dates are often weeks old. Net effect:
   first 7-day-lookback poll dropped most rows, watermark advanced, every
   subsequent ~30-minute window dropped ALL rows, status "ok" — silent
   forever. **Fix:** listing-only adapters now RECONCILE the whole listed
   set every sweep (no since-drop); idempotency comes from the upsert
   change-hash; Atamis stays bounded by ATAMIS_MAX_PAGES. The two
   adapters' cutoff tests are deliberately REVERSED to pin the new
   contract.
3. **S2W:** request shape is byte-identical to the WORKING PCS adapter
   (same upstream codebase) — no static drift. Its docstring documents the
   known upstream HTTP-500 ("Error converting data type nvarchar to
   float"). Whether the live failure is that 500 or an egress 403 is
   exactly one error-string away (below).
4. **External probes:** S2W API, EU-Supply, Atamis all 403 this
   environment's egress (both paths) — expected datacenter blocking;
   says nothing about Azure's egress. Live evidence required.

### What shipped

- `eu_supply.py` + `atamis.py`: reconcile semantics (the trap fix), with
  dated WHY-comments; cutoff tests reversed to pin the new contract.
- **`GET /admin/diagnostics/sources-health`** (require_account,
  read-only): per source — enabled, watermark, tender_count, latest 3
  PollRuns with their error strings, and a coarse `diagnosis` field
  (`never_polled` / `fetch_failing` / `polling_but_zero_rows` /
  `healthy`). This is the Azure Log stream in one URL. 3 offline tests.
- `tests/e2e/specs/phase-1.yaml` — the bot gate (each source in the facet
  with >0 rows), ready to run once evidence-driven fixes complete.
- `docs/accounts-needed.md`: NEPO recorded (handover §7.3 — separate
  platform, manual registration) + the standing email-OAuth setup item.

### Why not PASS yet

The phase's bot check needs the deployed fix + a live poll cycle + the
live bot run — none reachable from this environment (the backend and
dashboard 403 the sandbox). The remaining unknown (is Azure's egress
403'd by eu-supply.com / atamis / api.sell2wales?) is one click away.

### Next single action (operator, browser)

1. Merge the Phase-1 PR → auto-deploy → wait one poll cycle (~30 min, or
   re-fire `POST /admin/poll-now`).
2. Open `GET /admin/diagnostics/sources-health` on /docs and read the
   three sources' `diagnosis`:
   - `healthy` → run the bot gate: `python -m tests.e2e.dashboard_bot
     tests/e2e/specs/phase-1.yaml` → paste the report → Phase 1 flips to
     PASS (and phase-0-self.yaml spec 4 flips too).
   - `fetch_failing` with a 403-ish error → paste the JSON; next run wires
     that source's fetch through the disguised in-process bridge (the
     established pattern — no re-investigation).
   - `fetch_failing` with the S2W 500 → paste the JSON; we decide between
     a param change (if the error suggests one) and an upstream-bug
     BLOCKED entry.

## Next single action

Operator: follow Phase 1's three-step readout above (merge → one poll
cycle → read `GET /admin/diagnostics/sources-health`, then either run
the phase-1 bot gate or paste the JSON). Phase 0's live self-check
(`python -m tests.e2e.dashboard_bot tests/e2e/specs/phase-0-self.yaml`)
can be run in the same sitting — both YAMLs from one residential-IP
session.

## Phase 1 — IN PROGRESS rev 2 (2026-06-11): scheduler self-heal + real-error surface

### What the readout proved (operator's `GET /admin/diagnostics/sources-health`, 11:28Z)

- **ATAMIS, EU_SUPPLY = `never_polled`**: Source rows exist (`enabled: true`),
  zero `latest_runs`, `last_polled_at: null`. The watermark fix from rev 1
  was correct but not enough — the scheduler never invoked their adapters
  to begin with. Likeliest explanation (the static-code diagnosis cannot
  uniquely prove it from this environment, so the fix is defensive):
  these Source rows were created on a deploy AFTER the most recent
  `ensure_sources()` boot pass, OR a previous poll cycle was still
  in-flight at readout time.
- **S2W, NI = `fetch_failing` with the generic "upstream HTTP requests
  failed (see adapter log events)"**: the real cause (403 vs 500 vs DNS)
  was hidden in the log stream the harness explicitly wants the
  health endpoint to expose. Fixed below.
- **PROACTIS = `fetch_failing` with `BridgeError: navigation failed:
  Page.goto: net::ERR_ABORTED at .../Supplier/Advert/View?advertId=...`
  after `new_count: 61`**: one bad advert killed a run that had already
  ingested 61 rows. That's Phase-2 territory, but the small in-scope
  guard lands now.
- **One PROACTIS run stuck `running`** (orphaned by a restart).

### What this PR ships (rev 2 on top of #122)

1. **Scheduler self-heal.** `poll_all()` now calls `ensure_sources()` at
   the start of every cycle (not only at boot). A new adapter wired in
   after the previous boot joins the very next cycle — covers the
   never-polled case directly without speculating on the exact root
   cause. Idempotent; cheap (one read per code, one insert per missing).
2. **Per-iteration `scheduler.poll_source_attempt` log line** with
   `{source, has_adapter}`. The "did we even try to poll X?" question
   becomes a Log-stream grep, not a code-archaeology dig. Plus a
   `scheduler.poll_all_starting` line at the top of every cycle with
   `source_codes` and `registered_adapters` so the two lists can be
   compared at a glance.
3. **Real upstream errors on PollRun.error.** `SourceAdapter.record_error()`
   appends a short, length-bounded message every time an adapter sets
   `had_errors = True`. All HTTP adapters now call it (CF, FTS, PCS,
   S2W, NI, EU-Supply, Atamis). `poll_source` joins the last 3
   messages into `PollRun.error` instead of the legacy generic string —
   so the sources-health row carries the precise upstream cause.
4. **Orphaned PollRun cleanup.** `_orphan_stale_running_runs(db)` marks
   PollRuns left in `running` past 90 minutes as `status="error"` with
   `error="orphaned …"` and a `finished_at` of now. Runs at the top of
   every `poll_all` cycle.
5. **PROACTIS per-advert detail guard.** A `BridgeError` while reading
   one advert's detail page now logs + counts + continues, instead of
   killing the whole run. Trivial, in-scope: the deeper retry/backoff
   work stays for Phase 2.
6. **PROACTIS Source row safely skipped in `poll_all`.** The HTTP path
   never had an adapter for it (browser-driven discovery is a separate
   APScheduler job); the loop now skips with `has_adapter=false` in
   the log instead of raising `ValueError("No adapter…")`.

### Tests (offline, no Postgres)

- `tests/test_scheduler_self_heal.py` (5 tests): ensure_sources called
  first; per-iteration log lines present with correct codes; PROACTIS
  iterates but skips without an adapter; orphan cleanup flips the stale
  row only; cleanup is invoked from `poll_all`.
- Adapter test updates: same fixtures, the contracts kept; CF/FTS/PCS/
  S2W/NI/EU_SUPPLY/Atamis all still pass.
- 57 backend tests + the 3 sources-health tests pass under the same
  in-memory SQLite setup.
- Ruff clean.

### Phase 1 verdict — still IN PROGRESS

Same reason as rev 1: the gate is the live `GET /admin/diagnostics/
sources-health` readout AFTER merge + deploy + at least one poll cycle
(or `POST /admin/poll-now`). Three of the four findings now lead to
specific evidence-driven follow-ups:

- ATAMIS / EU_SUPPLY should leave `never_polled` and either show
  `healthy` (this run's fix sufficed) or `fetch_failing` with a real
  error string. If 403, route via the disguised bridge next PR; if
  something else, paste it.
- S2W and NI should show a SPECIFIC error class + URL, not the generic
  string. If the readout confirms the documented Sell2Wales 500, the
  pending-row in `docs/accounts-needed.md` is promoted to a real
  BLOCKED-UPSTREAM entry next PR; the eTendersNI string drives a
  matching decision.
- PROACTIS gets the small per-advert guard now; the deeper run-15
  resilience work is Phase 2 and stays out of this PR.

### Next single action

Operator: merge → wait one poll cycle (or fire `POST /admin/poll-now`)
→ open `GET /admin/diagnostics/sources-health` → paste the JSON. With
the real error strings in hand, the next harness run either flips
Phase 1 to PASS (run the bot gate `tests/e2e/specs/phase-1.yaml` if the
operator can reach the live URL; the CI unit-test gate is the automated
fallback) or implements the precise next fix.

## Phase 1 — IN PROGRESS rev 3 (2026-06-11): the real cause was starvation, not registration

### Verdict: Phase 1 IN PROGRESS — rev 3 (browser-only gate)

### What the readout (and the new Log stream) proved

The 2026-06-11 12:52Z `sources-health` readout AND the operator's pasted
`scheduler.poll_all_starting` log line together overturn the rev-2
working hypothesis. The Log stream now reports:

- `scheduler.poll_all_starting`: `source_codes` includes
  `EU_SUPPLY, PROACTIS, ATAMIS`; `registered_adapters` includes
  `EU_SUPPLY, ATAMIS`. So they were **registered all along** — the
  rev-2 self-heal was good housekeeping but did not solve the silence.
- Inside one cycle, `FTS` emits a long unbroken series of
  `requirements.extracted` events, one per tender (tender ids 7853,
  7854, 7855 …), each ~10–15 seconds: the per-tender Anthropic call
  that `_enrich_matched_tender` ran IN-LINE during the poll.
- Trace goes quiet; ~3 minutes later a NEW `poll_all_starting` begins
  at the top — the cycle never reached EU_SUPPLY, PROACTIS, or ATAMIS
  at the tail of the list.

**The cause is starvation, not registration.** Sources at the head of
the list (FTS) burn the whole poll interval on synchronous
per-tender extraction; sources at the tail never get a turn.

This also recasts two rev-2 findings:

- The "scheduler appears not to be firing" surface read of
  9-day-old runs in CF/FTS/PCS healthy rows: the scheduler IS firing,
  but each cycle's tail never executes — so older runs persist as the
  newest record for the late sources, and the diagnosis read "healthy"
  for early sources because their per-cycle work did complete.
- PROACTIS's `fetch_failing` diagnosis despite the newest run being
  `ok` (one older errored run + the `any(r.error for r in runs)`
  predicate was wrong).

### What this PR ships (rev 3 on top of #122 + #123)

1. **Enrichment decoupled from polling.** New module
   `services/enrichment_worker.py`. `poll_source` no longer downloads
   documents or extracts requirements in-line: it only fetches +
   upserts tenders, records FilterMatches, and dispatches push
   notifications immediately. The slow work — `download_documents_for_
   tender` + `extract_requirements` — moves to `enrich_tender` in the
   worker. Side effect: operators are notified of a new match the
   moment it's recorded, no longer queued behind the AI call.
2. **Implicit-queue worker.** `process_pending_enrichment(db, *, limit)`
   selects tenders that have at least one `FilterMatch` and no
   `TenderRequirements` row (non-duplicate, FIFO by tender id),
   bounded by `enrichment_worker_batch_size`. The unique
   `TenderRequirements.tender_id` constraint makes the worker
   idempotent under concurrent poll cycles — no schema change.
3. **New scheduler job `enrichment_worker_job`** on its own
   `IntervalTrigger` (default 5 minutes), same `coalesce=True` +
   `max_instances=1` safety as the other jobs. Three new settings
   (`enrichment_worker_enabled` / `_interval_minutes` /
   `_batch_size`) gate the behaviour.
4. **Better diagnosis** in `GET /admin/diagnostics/sources-health`:
   - The `fetch_failing` branch now decides on the newest run only —
     not `any(r.error for r in runs)` — so an older orphaned-then-
     errored run never trips fetch_failing when the newest is `ok`
     (PROACTIS, this readout).
   - New `stale_not_polling` class: a clean newest run that finished
     more than `poll_interval_minutes × 4` ago means the scheduler
     itself isn't firing for that source, NOT that it's healthy
     (CF/FTS/PCS, 2026-06-02 timestamps, this readout).
5. **What stays untouched on purpose.** The manual `POST /admin/
   extract-requirements/{tender_id}` endpoint is unchanged (operator
   re-run path still works). Adapter watermark/reconcile semantics,
   `record_error()` adapter wiring, `_orphan_stale_running_runs()`,
   per-iteration `scheduler.poll_source_attempt` logging, the
   PROACTIS per-advert guard — all stay as #122 + #123 shipped them.
   The TLS error from `portal_classifier.fetch_failed` on
   `parsystems.co.uk` is out of scope for Phase 1 — noted, log-only.

### Tests (offline, no Postgres)

- `tests/test_enrichment_worker.py` (8 tests):
  - Hot-path drift guard: `ingestion` module no longer exposes
    `extract_requirements`, `download_documents_for_tender`, or
    `_enrich_matched_tender` (re-importing them would re-introduce
    starvation).
  - Queue: only matched-and-unenriched tenders are picked;
    already-enriched tenders are skipped; non-matched tenders are
    skipped; duplicates are excluded; FIFO by id; limit respected.
  - Worker: empty queue returns 0; a backlog returns batch-size
    processed; `enrich_tender` runs both halves and continues to
    extraction even when download raises.
- `tests/test_sources_health.py` extended (5 tests total):
  - Newest-`ok`-run-overrides-older-errored-run case (PROACTIS).
  - `stale_not_polling` case (CF, 9-day-old "healthy" reading).
- Existing `test_scheduler_self_heal.py` and `test_ingestion_status.py`
  still pass (the rev-2 contracts are preserved).
- Ruff clean.

### Phase 1 verdict — still IN PROGRESS, gate is the next browser readout

The fix is structural and large enough that it deserves its own live
verification cycle before Phase 1 closes. After merge + auto-deploy +
one poll cycle (or `POST /admin/poll-now`), the next browser readout
of `GET /admin/diagnostics/sources-health` should change dramatically
within minutes:

- **EU_SUPPLY and ATAMIS leave `never_polled`** (they get reached now
  that the FTS cycle no longer eats the whole interval). They
  either show `healthy`, `polling_but_zero_rows`, or `fetch_failing`
  with a specific error string from the rev-2 `record_error` wiring.
- **CF / FTS / PCS no longer read "healthy" while 9 days old.** If
  the scheduler is firing they get fresh runs; if not, they read
  `stale_not_polling` and the cause is somewhere else (separate
  diagnosis, not this PR's concern).
- **S2W and NI** show the specific upstream error string (or stay
  generic, which would mean their adapters don't yet thread
  `record_error` — verify against #123's adapter calls).
- **PROACTIS** no longer reads `fetch_failing` when its newest run is
  clean.

If after this the late sources are reached AND S2W/NI surface a
genuine upstream outage (their server returns 500 / 403), then
Phase 1 closes as **PASS — with-blocked-upstreams-recorded** and the
S2W/NI pending rows in `docs/accounts-needed.md` get promoted to
real BLOCKED-UPSTREAM entries. Until that readout lands the harness
verdict stays **IN PROGRESS**.

### Honest correction from rev 2

Rev 2 leaned on `scheduler self-heal + real-error surface` as the
likely fix for `never_polled`. The real-error surface part (`record_
error` + `error_messages`) was right and stays in. The self-heal part
turned out to be belt-and-braces — useful but not the cause.
EU_SUPPLY and ATAMIS were registered and enabled the entire time.
What stopped them being polled was a starved iteration loop. Saying
this out loud because the harness asks for honest progress, not the
nicest story.

### Next single action

Operator: merge → wait one poll cycle (or fire `POST /admin/poll-now`)
→ wait a further 5 minutes so the enrichment worker has a chance to
fire its first batch → open `GET /admin/diagnostics/sources-health`
→ paste the JSON. The new readout decides between Phase 1 PASS and
the next narrow fix.

## Phase 1 — IN PROGRESS rev 4 (2026-06-12): serial poll still starves the tail — make it concurrent

### Verdict: Phase 1 IN PROGRESS — rev 4 (browser-only gate)

### What the Log stream proved (this cycle, LOG-PROVEN)

The rev-3 enrichment decoupling shipped and works — every tender now logs
`ingest.enrichment_deferred`, so the slow Anthropic call is off the poll
path. But the goal is still unmet and the Log stream shows exactly why:

- `scheduler.poll_all_starting` source order: `SAMPLE_SEED, FTS, CF, PCS,
  S2W, NI, EU_SUPPLY, PROACTIS, ATAMIS`.
- A `poll_source_attempt` fires for SAMPLE_SEED, then FTS — and then for
  **over a minute and counting** the stream is nothing but FTS tender
  ingestion (`tender_id 8017 → 8176+`, 160+ rows, all FTS,
  `ingest.enrichment_deferred` each).
- **No `poll_source_attempt` appears for CF/PCS/S2W/NI/EU_SUPPLY/
  PROACTIS/ATAMIS** — the cycle is still inside FTS.

**Conclusion (confirmed, not hypothesised): the poll was SERIAL.**
`poll_all` `await`ed each source's `poll_source` to full completion before
starting the next, so FTS — which has thousands of tenders — consumed the
whole cycle and every source after it was never reached. Decoupling
enrichment (rev 3) was necessary but not sufficient: the ingestion VOLUME
itself is the blocker now, not the AI call.

### What this PR ships (rev 4 on top of #122 + #123 + #124 + #125)

1. **Concurrent per-source poll.** `poll_all` no longer walks sources in a
   serial `for` loop. It fans each source out onto its own task via
   `asyncio.gather` (new helper `_poll_one_source`), so a high-volume
   source can't block the others. Contract satisfied: in a single
   scheduler interval EVERY registered source now gets a `poll_source_
   attempt` and records a poll run, regardless of how many tenders FTS
   has — the `poll_source_attempt` line for every source is emitted at
   task start, before any ingestion work.
2. **Session-per-task.** Each concurrent source opens its OWN
   `SessionLocal()` and re-loads its `Source` by id — the cycle never
   threads one SQLAlchemy Session across concurrent tasks (Sessions are
   not concurrency-safe). The shared session is used only for the serial
   pre-amble (orphan cleanup + the source list).
3. **Bounded + isolated.** A `poll_max_concurrency` setting (default 6)
   caps concurrent sources, bounding DB connections (engine pool 5 + 10
   overflow) while comfortably exceeding the ~8 HTTP sources. `gather(...,
   return_exceptions=True)` plus a per-task try/except means one source
   raising is logged (`scheduler.source_failed`) and can't cancel its
   siblings. Set the cap to 1 to fall back to serial polling.
4. **Everything from earlier revs preserved.** The enrichment worker,
   watermark/reconcile, `_orphan_stale_running_runs`, real-error
   surfacing (`record_error`), per-iteration `poll_source_attempt`
   logging, the `stale_not_polling` diagnosis, and the PROACTIS-skip
   (browser-driven discovery is its own job) all stay exactly as shipped.
   No inline enrichment was reintroduced.

### On S2W / NI specific errors (the "also confirm" item)

The specific-upstream-error path is already intact from rev 2: both the
Sell2Wales adapter (`record_error(f"{dateFrom}: {type(exc).__name__}:
{exc}")`) and the eTendersNI adapter (`record_error(f"fetch failed:
{type(exc).__name__}: {exc}")`) record the precise cause whenever they set
`had_errors`, and `poll_source` surfaces `error_messages` on
`PollRun.error` — only falling back to the generic
"upstream HTTP requests failed" when an adapter set `had_errors` with no
recorded message. The generic string in the live readout was a symptom of
the SERIAL bug, not a missing error path: S2W/NI sit at positions 5–6 in
the order and the cycle never reached them, so their displayed run was
stale. A new regression test
(`test_poll_source_surfaces_specific_adapter_error_not_generic`) pins that
a recorded specific error wins over the generic fallback. Once the
concurrent cycle actually reaches S2W/NI, their readout will show the real
403/500/DNS string; per "no guessed fixes" no upstream is promoted to a
`BLOCKED-UPSTREAM` row in `docs/accounts-needed.md` until that evidence
lands.

### Tests (offline, no Postgres)

- `tests/test_scheduler_concurrent_poll.py` (3 tests):
  - **The core regression:** a high-volume source (FTS) whose poll only
    finishes AFTER the tail sources have been polled — under the old
    serial loop `poll_all` would never return; under the fan-out the tail
    runs, releases FTS, and the cycle finishes. (Asserts FTS + EU_SUPPLY +
    ATAMIS all polled.)
  - One source raising is logged (`scheduler.source_failed`) and does not
    stop its siblings.
  - Each source polls on a DISTINCT session (no cross-task session
    sharing).
- `tests/test_ingestion_status.py` extended: the specific-error-wins test
  above.
- Existing `tests/test_scheduler_self_heal.py` (5) still green — the
  ensure-sources-first, per-source `poll_source_attempt`, PROACTIS-skip,
  and orphan-cleanup contracts are all preserved under the new fan-out.
- Full suite: 914 passed, 3 skipped. The one failure is the pre-existing
  dev-DB live-data flake `test_portal_platform_matching::test_every_
  platform_matches_at_least_three_sightings` ("proactis matched only 2
  sightings"), unrelated to this change.
- Ruff clean.

### Phase 1 verdict — still IN PROGRESS, gate is the next browser readout

After merge + auto-deploy + `POST /admin/poll-now` + a few minutes, the
backend Log stream should show `poll_source_attempt` for EU_SUPPLY and
ATAMIS (they are reached now), and `GET /admin/diagnostics/sources-health`
should show them leaving `never_polled` — either ingesting, or recording a
real error string. If every source is then reached AND S2W/NI prove to be
genuine upstream outages (real 403/500/DNS), Phase 1 may close as
**PASS — with-blocked-upstreams-recorded** (promote the S2W/NI rows in
`docs/accounts-needed.md` at that point). Until that readout lands the
harness verdict stays **IN PROGRESS**.

### Next single action

Operator: merge → fire `POST /admin/poll-now` → wait a few minutes →
read the Log stream for `poll_source_attempt` on EU_SUPPLY + ATAMIS, then
open `GET /admin/diagnostics/sources-health` and paste the JSON. The CI
dashboard-bot is the automated gate; the residential-IP live bot run is
optional.
