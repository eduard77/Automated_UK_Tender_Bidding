# Genera Tenders — Phased Build & Verify Harness

Read `docs/genera-handover.md` (the handover) first — it is the source of truth for
what to build and in what order. This prompt turns it into a **gated, phase-by-phase
build with automated verification**.

## How this works (read carefully — it defines the whole run)

- Work **one phase at a time, in order.** On each invocation, find the **first phase
  not yet marked DONE** (track status in `docs/build-progress.md` — create it if
  absent) and attempt ONLY that phase.
- After building a phase, **verify it with the reusable dashboard bot** (Phase 0
  builds the bot) and any backend tests. A phase is DONE only when its tests pass.
- **Do not start the next phase** until the current one is DONE. Hard gate.
- End every run by writing a **VERDICT** (below) to `docs/build-progress.md` and the
  PR description, then STOP. Do not chain into the next phase in the same run — open
  a PR for review first. (One phase ≈ one PR ≈ one run.)

### The three verdicts at each gate
- **PASS** — phase built, all its tests green. State: "Phase N done; next is Phase N+1."
- **BLOCKED — NEEDS ACCOUNT** — cannot proceed without the operator registering on a
  portal/service. Append it to `docs/accounts-needed.md` (see below) and STOP.
- **NEEDS DECISION** — this phase is a **product/design choice**, not a coded task.
  Present the options with a recommendation and STOP. Do NOT guess a product
  direction or build one speculatively.

### Discipline (learned the hard way — non-negotiable)
- **Diagnose before fixing.** If something doesn't work, capture the real
  state/markup first (a diagnostic), then fix from evidence. Never guess selectors,
  formats, or schemas.
- **Verify merges/symbols actually landed** (phantom-merge risk): after writing code,
  confirm the expected symbol is present before claiming done.
- **A failing test you don't understand → STOP and report**, don't auto-retry blindly.
- **Self-correction is allowed only for clear, understood failures** on coded tasks
  (e.g. a wrong selector the diagnostic just revealed). Anything ambiguous → report.
- Datacenter 403s on portals are expected; the established fix is the disguised
  in-process browser bridge — apply that pattern, don't re-investigate from scratch.

### The accounts-needed report (`docs/accounts-needed.md`)
Maintain a running list. Whenever any phase discovers a source that **requires manual
registration / login the operator must do** (no automated signup — email verify, 2FA,
ToS acceptance), append an entry: portal name, URL, why it's needed, what it unlocks,
and any known signup notes. This is a deliverable in its own right — the operator will
work through it between runs.

---

## PHASE 0 — Build the reusable dashboard bot (do this first)

A Playwright-based **dashboard verification bot** that lives in the repo
(`tests/e2e/dashboard_bot/` or similar) and is **reusable by every later phase** as
its acceptance check. It must:
- Drive the LIVE dashboard like a human (navigate, click filters, type, submit, read
  results) — not hit the API directly; the point is to verify the real user path.
- Be parameterisable: given a check spec (e.g. "filter Source=Proactis, expect >0
  rows", "filter CPV=45000000, expect construction tenders", "open a tender, expect a
  CPV field present"), run it and return pass/fail with the observed result.
- Mimic reasonable human pacing; handle the cross-site session/login.
- Produce a readable report (which checks passed/failed, observed vs expected).
- Be runnable headless in CI and on demand.
**Phase 0 tests:** the bot runs against the current dashboard and correctly reports the
KNOWN current state (e.g. Proactis rows exist; CPV filter currently returns nothing for
Proactis — the bot should report that truthfully). Gate: bot runs green and its
self-tests pass.

---

## PHASES — derived from the handover (build in this order)

For each phase below: build it, then write/extend a **bot check** that proves it, run
it, and gate. Where a phase is a product decision, return **NEEDS DECISION** with
options instead of building.

### PHASE 1 — Get the silent sources pulling (Atamis, EU-Supply, Sell2Wales)
Diagnose first (Log stream / direct probes) why each isn't ingesting:
`*.fetch_failed` (datacenter 403 → disguised bridge), `*.pagination_stalled`
(pagination fix), or no scheduler activity (registration). Fix what's fixable in code.
Any source that needs a **manual account** → BLOCKED-NEEDS-ACCOUNT entry.
**Bot check:** each fixed source appears in the dashboard Source facet with >0 rows.

### PHASE 2 — Fix run-15-class discovery errors
Investigate the Proactis `profile_failed` mid-run error (errored at page 17 after
inserting rows). Make discovery resilient (don't lose a whole run on one bad page).
**Bot check:** a full Proactis run completes with status ok and inserts rows.

### PHASE 3 — Cross-source CPV/field normalisation (THE SPINE) — likely NEEDS DECISION
The core requirement: every tender from every source lands with the same filterable
fields (CPV, region, value). Per the handover, Proactis has no CPV on listings; CF/FTS
do; Atamis/EU-Supply TBD. The **approach is a product/design choice** (stamp searched
CPV at save-time vs fetch from detail pages vs accept-and-lean-on-CF/FTS).
→ Return **NEEDS DECISION** with the options, a recommendation (stamp-at-save is the
likely sweet spot), and the trade-offs. Build only after the operator picks.
Once chosen: implement, and **bot check:** filtering the dashboard by CPV 45000000
returns the known construction Proactis tenders.

### PHASE 4 — Keyword (include/exclude), value, buyer filters as first-class
Make keywords (include AND exclude), value thresholds, and buyer include/exclude work
end to end on profiles and in the dashboard. **Bot check:** each filter narrows results
correctly against seeded expectations.

### PHASE 5 — Per-tender state (interested / not-interested / applied)
Per-user state so digests and views stop re-showing rejected tenders. (Note: true
per-user needs Phase 8 multi-tenant; build the state model now, scope to operator until
then.) **Bot check:** marking a tender hides/labels it appropriately on next view.

### PHASE 6 — Guided setup page (word → CPV picker → save) — partly NEEDS DECISION
The setup UI: type a word, see related CPV codes (needs the public CPV list in the
repo), pick them, set region/value/keywords, **include/exclude sources**, save as a
named dashboard. Offer **CPV presets/bundles** (users don't know their codes).
The *mechanism* is buildable; the **UX/preset taxonomy is a design choice** → present a
proposed design as NEEDS DECISION before full build, then implement on approval.
**Bot check:** completing setup creates a profile that drives a correctly-filtered
dashboard.

### PHASE 7 — Daily digest notifications
Batch new matching tenders into a daily notification (email and/or push). Decisions to
surface (NEEDS DECISION where open): send time/timezone, channel, first-setup backlog
handling, "nothing new today" behaviour, deadline reminders. **Bot/test check:** a
simulated day's new tenders produces one correct digest per profile, deduped.

### PHASE 8 — Multi-tenant (accounts, isolation, billing) — NEEDS DECISION (big)
Real user accounts, login, strict per-user data isolation, billing. This is a project
in itself. Return **NEEDS DECISION** with a scoped proposal (auth approach, data
isolation model, billing integration) — do NOT build speculatively. Flag legal/ToS/data-
protection items for the operator.

---

## Output of every run
1. Update `docs/build-progress.md`: which phase attempted, VERDICT, what's next.
2. Update `docs/accounts-needed.md` if any account blockers were found.
3. Open a PR for the phase's work (if any code) with the VERDICT as its first line.
4. STOP. Summarise for the operator: phase, verdict, the single next action
   (advance / register-then-advance / decide-then-advance), and — if NEEDS DECISION —
   the options and your recommendation.

Begin with **Phase 0** (the bot). Do not attempt later phases in this run.
