# Build progress

This file tracks the Genera Tenders **phased build-and-verify harness** described in
the operator's run prompt. One phase per PR; later phases are gated on the previous
phase being marked DONE.

## Current verdict

| Phase | Title | Verdict | Date | Notes |
| --- | --- | --- | --- | --- |
| 0 | Reusable dashboard bot | **NEEDS DECISION** | 2026-06-11 | Handover doc missing — see below. |

## Phase 0 — NEEDS DECISION (2026-06-11)

The prompt's first instruction is:

> Read `docs/genera-handover.md` (the handover) first — it is the source of truth
> for what to build and in what order.

That file **does not exist** in this repository. The full search:

```
$ find . -path ./node_modules -prune -o -path ./.venv -prune \
       -o -iname "*handover*" -print
(no matches)
```

Without it the run cannot proceed in a way that satisfies its own discipline rules:

* *"Diagnose before fixing. Never guess selectors, formats, or schemas"* — the bot's
  self-tests must assert the **known current state**, which the handover defines. I
  cannot pick those assertions without inventing them.
* *"When the spec is silent, ask rather than invent"* (CLAUDE.md §9). The handover is
  the spec for every phase that follows; guessing its contents would mis-shape Phase 1
  onwards (which sources to investigate, which UI flows the bot should drive, what
  pass criteria mean).

### What I need from the operator

Drop the handover at `docs/genera-handover.md` (or tell me where it lives) and re-run
this prompt. The handover should make these explicit so the bot's first check spec is
unambiguous:

1. The deployed dashboard URL the bot should drive (the most recent `feat(deploy)`
   work points at the `genera-tenders-dashboard…azurewebsites.net` shape — please
   confirm the exact host).
2. Whether the bot logs in (and if so, the credentials path — env var, GitHub
   secret, etc.) or runs against an anonymous path.
3. The **known current state** the bot's self-tests should pin — e.g. "Proactis rows
   exist", "CPV filter currently returns nothing for Proactis", etc. That is exactly
   what makes Phase 0's gate honest rather than a placeholder.
4. The ordered phase list (this prompt repeats it but the handover is named as the
   authoritative copy — if they ever disagree, the handover wins).
5. Anything Phase 0 must *not* do (e.g. don't drive write-paths, don't store cookies).

## Reusable scaffolding NOT introduced this run (deliberately)

I have NOT created `tests/e2e/dashboard_bot/` skeleton files or a Playwright config
yet. Doing so before knowing what the bot is meant to assert would either:

* lock in a shape (headless config, login model, fixture format) that the handover
  later contradicts and forces a rewrite, or
* generate placeholder self-tests that pass trivially — the exact "phantom merge"
  failure mode the discipline section calls out.

Once the handover lands, the very next PR will be Phase 0 proper: a Playwright bot
under `tests/e2e/dashboard_bot/` that accepts parameterised check specs, runs against
the live URL, and ships with self-tests pinned to the handover's "known current
state". The browser-bridge work already in the repo (Chromium 1.47, disguised UA,
session handling) carries straight over so the bot can reuse the proven cloud-fetch
shape instead of re-investigating it.

## Next single action for the operator

Provide the handover (`docs/genera-handover.md`) and re-run the build-and-verify
prompt. I will then resume Phase 0 with the bot's self-tests anchored to the real
acceptance criteria.
