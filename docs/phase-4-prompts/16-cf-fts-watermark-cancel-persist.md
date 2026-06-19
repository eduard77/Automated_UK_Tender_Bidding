# 16 — CF/FTS watermark still not persisting on timeout: DIAGNOSE before fixing

> Saved 2026-06-15. Third attempt at the same problem. Do **not** jump to a fix —
> diagnose with evidence first, then fix only the proven cause.

## The symptom, precisely (from live source-health diagnostics, 2026-06-15)

- CF and FTS poll runs time out at 900s and are cancelled by the scheduler self-heal.
- Across successive cycles, the tender counts keep climbing (FTS 6,693 → 6,777;
  CF 5,823 → 5,891 over a few cycles) — so records ARE being committed, even on
  timed-out runs.
- BUT every timed-out CF/FTS run shows `watermark_at: null`, and FTS
  `last_polled_at` is still stuck at 2026-06-02 — the watermark/resume pointer is
  NOT advancing.
- Result: every cycle re-walks the same backlog from the same start, re-fetching
  tenders it has already seen (which is why counts creep up slowly rather than the
  backlog draining), and times out again. Infinite non-draining loop.

PR #133 was supposed to fix exactly this by advancing/committing the watermark
per-page so a timeout resumes forward. The FTS `salvaged_continued` log proves
#133's code IS deployed and running. Yet the watermark still doesn't persist on
the timeout path. So #133's mechanism is not working in practice.

## Phase 1 — DIAGNOSE (do this first, report findings, do not change behaviour yet)

Investigate and state with evidence which of these is true (leading suspicion first):

1. **Transaction rollback on cancellation.** The per-record commits land on their
   own transaction, but the watermark UPDATE is in a transaction/session that is
   still open when the 900s `asyncio.wait_for` fires `CancelledError`. The
   cancellation tears down the task mid-transaction, so the uncommitted watermark
   write is rolled back — while already-committed record rows survive. Verify by
   tracing: in CF and FTS poll paths, is the watermark write committed in the same
   flush/commit as records, or separately? Is there an open transaction at the
   await point where cancellation lands?
2. **Watermark write is after the loop, not per-page.** Despite #133's intent,
   confirm whether the watermark advance for CF (and FTS) actually executes
   per-confirmed-page inside the loop, or whether it still only runs at end-of-run
   (which the cancel never reaches). Check both adapters separately — they may
   differ; #133 may have only correctly wired one.
3. **`CancelledError` bypasses the persistence block.** Confirm whether the
   watermark-persist code sits under an `except Exception` (which does NOT catch
   `CancelledError`, since that derives from `BaseException`) or a `finally`. If
   it's in `except Exception`, the timeout path skips it entirely — same class of
   bug as the original #131 root cause.

Report which one(s) are real, with the specific code path, before touching logic.

## Phase 2 — FIX only the proven cause

Make the watermark/resume pointer durably advance for every page whose records were
committed, such that a 900s cancellation keeps the watermark in lockstep with the
committed records. The watermark write must be committed atomically with (or
immediately after, in its own committed transaction) the records for that page —
never left in an open transaction that cancellation can roll back, and never in an
`except Exception` block that `CancelledError` skips. Apply to BOTH CF and FTS;
verify each independently rather than assuming shared code.

After the fix: a timed-out run must leave `watermark_at` set to the last committed
page's position, and the next run must resume strictly forward from there. Over
successive cycles the backlog must actually shrink (counts stop re-creeping over
old data; FTS `last_polled_at` climbs off 2026-06-02 toward today).

**Verification:** offline test that simulates the exact failure — inject an
`asyncio.CancelledError` mid-sweep after N pages have committed, then assert the
watermark reflects page N (not null, not start) AND the next run resumes from page
N+1. Parametrised for CF and FTS. Plus a test proving the persistence path is
reached under `CancelledError` specifically. Full suite passes, ruff clean,
additive-only migration if any.

**Out of scope:** NI (upstream 500), S2W (expired cert), Proactis (manual browser).
Do not touch classification.

Open a PR with VERDICT on the first line and, in plain English, state exactly which
of the three diagnoses was the real cause.
