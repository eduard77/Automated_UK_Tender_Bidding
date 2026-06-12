"""Classification backfill over the existing unclassified pool — hardened.

Uses the Anthropic **Message Batches API** (≈50% cheaper than per-call, and
the backlog isn't time-sensitive). Hardened after the 2026-06-12 incident:

- The first 10k run submitted ONE 10,000-request batch and died ~40 minutes
  in on an unguarded exception that was only visible as ``exc_info`` — which
  the Azure log stream doesn't render — so the actual error was unknown. The
  run ABANDONED its batch on Anthropic's side with no tracking or recovery,
  and every subsequent run failed within seconds.
- PROVEN code defects, fixed here: no exception handling anywhere in the run
  (one transient raise lost everything), an unbounded single submission, no
  awareness of previously-submitted batches (abandonment + re-buying the same
  tenders), and invisible errors.
- The EXACT mechanism of the instant create failures is deliberately left as
  hypothesis, not fact: an abandoned in-flight batch is handled by the
  self-heal below, but documented Batch API queue limits (100k requests in
  queue even at the lowest tier) mean queue occupancy ALONE should not block
  a 500-request create — the true rejection error will be captured by the
  structured error fields this revision adds (`error_type` / `error` on
  `classification.backfill_failed`, phase="submit") the next time it fires.
  A second candidate — a poisoned tender aborting JSON serialisation — is
  closed by the UTF-8 sanitiser in ``taxonomy.build_user_text``.

Design (rev 2):

- **Chunked submission.** A large `limit` is split into sequential chunks of
  `classifier_backfill_chunk_size` (default 500 — the size production proved;
  far under the Batch API caps of 100k requests / 256 MB per batch). Each
  chunk is submitted, polled to completion, and collected before the next is
  created, so at most ONE batch of ours is ever in flight and a failure loses
  at most one chunk's wait — never the run's committed progress.
- **Per-row commit + containment.** `collect_results` commits each tender as
  its result lands; a stream failure mid-collect keeps everything already
  written AND keeps the partial counters (returned with an error marker, so
  the operator's summary never under-reports committed work).
- **Self-heal on start.** Before submitting anything, the run lists recent
  batches on the API: a still-in-flight batch is ADOPTED (polled + collected
  instead of creating a competing one), and recently-ENDED batches of OURS
  (custom_id `tender-<id>`) are collected — including partially-collected
  ones from a mid-collect crash (per-row skip-current makes re-collection
  idempotent, and results retrieval is unbilled). Abandoned API state also
  self-expires (processing ≤24 h; results 29 days), so there is no
  permanently-stuck state and nothing to clean in our DB — no schema change.
- **No double-buying.** Tender ids submitted once in a run are never
  re-submitted by a later chunk of the same run (errored rows stay on the
  watermark for a later run / the worker instead of burning the limit).
- **Per-phase error containment.** Every phase (recover / adopt / select /
  submit / poll / collect) is guarded; failures return a structured summary
  and log `classification.backfill_failed` with the exception type and
  message as PLAIN FIELDS plus context (phase, chunk, counts, batch id) —
  never only exc_info.
- Still bounded + resumable + idempotent on the `classifier_version`
  watermark: re-running drains what's left and never re-submits rows already
  on the current version.

Single-key assumption: in-flight batches cannot be ownership-checked (their
custom_ids are unreadable until they end), so adoption assumes the Anthropic
API key is dedicated to this service. A foreign adopted batch degrades to one
wasted wait budget — after it ends, the ownership peek skips its results.

The client and the sleep function are injectable so the whole flow is
testable offline against a fake batch client.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import structlog

from tender_agent.config import settings
from tender_agent.db import SessionLocal
from tender_agent.models import Tender
from tender_agent.services.classification.classifier import (
    _MAX_TOKENS,
    ClassificationResult,
    _make_client,
    _response_text,
    _usage,
    apply_classification,
    parse_classification,
    pending_classification,
)
from tender_agent.services.classification.taxonomy import (
    CLASSIFIER_VERSION,
    OTHER_SECTOR,
    build_user_text,
    system_prompt_blocks,
)

logger = structlog.get_logger(__name__)

#: How many recent batches the self-heal scan inspects, and how far back it
#: looks. The real SDK's list() auto-paginates over the org's whole batch
#: history when iterated, so BOTH bounds are load-bearing. Abandoned batches
#: only matter for the recent-failure scenario; the API itself expires
#: processing at 24 h and results at 29 days.
RECOVERY_SCAN_LIMIT = 20
RECOVERY_WINDOW = timedelta(hours=48)

#: Batch ids already collected (or confirmed foreign) THIS PROCESS — avoids
#: re-streaming the same ended batch's results on every run start. Process-
#: lifetime on purpose: across restarts a one-off re-stream is harmless
#: (per-row skip-current) and bounded by the recovery window; persisting it
#: would need schema. Never blocks recovery — a failed collection is not
#: added, so the next run retries it.
_handled_batch_ids: set[str] = set()


@dataclass
class BackfillSummary:
    submitted: int = 0
    classified: int = 0
    #: Rows written back from ADOPTED/RECOVERED batches a previous (failed)
    #: run abandoned — work that was already paid for.
    recovered: int = 0
    errored: int = 0
    skipped_no_tender: int = 0
    #: Result rows whose tender was already on the current classifier version
    #: (an idempotent re-collection, or the worker classified it meanwhile).
    skipped_already_current: int = 0
    chunks_submitted: int = 0
    chunks_completed: int = 0
    batch_id: str | None = None  # last batch touched
    batch_ids: list[str] = field(default_factory=list)
    #: "ok" | "empty" | "pending" (a batch is still processing past max_wait —
    #: the next run adopts it via self-heal) | "no_api_key" | "error"
    status: str = "ok"
    #: On status="error": which phase failed, and the exception as plain text.
    phase: str | None = None
    error: str | None = None
    error_type: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0


def _custom_id(tender_id: int) -> str:
    return f"tender-{tender_id}"


def _tender_id_from_custom_id(custom_id: str) -> int | None:
    if not custom_id or not custom_id.startswith("tender-"):
        return None
    try:
        return int(custom_id.split("-", 1)[1])
    except (ValueError, IndexError):
        return None


def build_batch_request(tender: Tender) -> dict:
    """One Batch-API request for one tender — same model/prompt/params as the
    per-call path (so caching applies across the batch too)."""
    return {
        "custom_id": _custom_id(tender.id),
        "params": {
            "model": settings.classifier_model,
            "max_tokens": _MAX_TOKENS,
            "temperature": 0,
            "system": system_prompt_blocks(),
            "messages": [
                {
                    "role": "user",
                    "content": build_user_text(
                        tender.title, tender.description, tender.buyer_name
                    ),
                }
            ],
        },
    }


def _fail(
    summary: BackfillSummary,
    phase: str,
    exc: Exception | None = None,
    *,
    error_type: str | None = None,
    error: str | None = None,
    **context,
):
    """Stamp the failure onto the summary and emit ONE loud, fully-structured
    event. The exception type and message are plain fields — the 2026-06-12
    incident proved exc_info alone is invisible in the Azure log stream."""
    summary.status = "error"
    summary.phase = phase
    summary.error_type = error_type or (type(exc).__name__ if exc else "unknown")
    summary.error = (error or (str(exc) if exc else ""))[:500]
    log = logger.exception if exc is not None else logger.error
    log(
        "classification.backfill_failed",
        phase=phase,
        error_type=summary.error_type,
        error=summary.error,
        submitted=summary.submitted,
        classified=summary.classified,
        recovered=summary.recovered,
        chunks_submitted=summary.chunks_submitted,
        chunks_completed=summary.chunks_completed,
        batch_id=summary.batch_id,
        **context,
    )
    return summary


def collect_results(db_factory, batch_id: str, *, client=None) -> BackfillSummary:
    """Stream an ENDED batch's results and write each classification back.

    Per-row commit so progress is never lost; per-row error containment so one
    bad row can't poison the rest of the batch; rows already on the current
    classifier version are skipped (cheap idempotent re-collection). A
    STREAM-level failure (the results iterator raising mid-flight) does not
    discard the counters accumulated so far: the partial summary is returned
    with an error marker so callers and the operator never under-count
    committed work."""
    client = client or _make_client()
    summary = BackfillSummary(batch_id=batch_id)
    try:
        with db_factory() as db:
            for entry in client.messages.batches.results(batch_id):
                tender_id = _tender_id_from_custom_id(
                    getattr(entry, "custom_id", "")
                )
                if tender_id is None:
                    continue
                try:
                    tender = db.get(Tender, tender_id)
                    if tender is None:
                        summary.skipped_no_tender += 1
                        continue
                    if tender.classifier_version == CLASSIFIER_VERSION:
                        summary.skipped_already_current += 1
                        continue
                    result = getattr(entry, "result", None)
                    if getattr(result, "type", None) != "succeeded":
                        summary.errored += 1
                        logger.warning(
                            "classification.backfill_result_errored",
                            batch_id=batch_id,
                            tender_id=tender_id,
                            result_type=getattr(result, "type", None),
                        )
                        continue
                    message = getattr(result, "message", None)
                    in_tok, out_tok = _usage(message)
                    summary.input_tokens += in_tok
                    summary.output_tokens += out_tok
                    parsed = parse_classification(_response_text(message))
                    if parsed is None:
                        # Garbled batch output → Other (don't fail the tender).
                        parsed = ClassificationResult(
                            primary_sector=OTHER_SECTOR, valid=False
                        )
                    apply_classification(tender, parsed)
                    db.commit()
                    summary.classified += 1
                except Exception as exc:  # noqa: BLE001 — one bad row mustn't kill the batch
                    db.rollback()
                    summary.errored += 1
                    logger.warning(
                        "classification.backfill_row_failed",
                        batch_id=batch_id,
                        tender_id=tender_id,
                        error_type=type(exc).__name__,
                        error=str(exc)[:500],
                    )
    except Exception as exc:  # noqa: BLE001 — stream-level failure: keep partial counts
        summary.status = "error"
        summary.phase = "collect_stream"
        summary.error_type = type(exc).__name__
        summary.error = str(exc)[:500]
        logger.warning(
            "classification.backfill_collect_stream_failed",
            batch_id=batch_id,
            error_type=summary.error_type,
            error=summary.error,
            classified_before_failure=summary.classified,
            errored=summary.errored,
        )
    return summary


def _merge_collected(
    summary: BackfillSummary, collected: BackfillSummary, *, recovered: bool
) -> None:
    """Fold one batch's collection counters into the run summary. The rows
    are committed per-row, so these counters are real even when the
    collection itself ended in a stream failure."""
    if recovered:
        summary.recovered += collected.classified
    else:
        summary.classified += collected.classified
    summary.errored += collected.errored
    summary.skipped_no_tender += collected.skipped_no_tender
    summary.skipped_already_current += collected.skipped_already_current
    summary.input_tokens += collected.input_tokens
    summary.output_tokens += collected.output_tokens


def _wait_until_ended(
    client, batch_id: str, initial_status: str | None, *,
    poll_interval_s: float, max_wait_s: float, sleep,
) -> str | None:
    """Poll one batch until it ends or the per-batch wait budget runs out.
    Returns the last seen processing_status."""
    status = initial_status
    max_polls = (
        max(1, int(max_wait_s / poll_interval_s)) if poll_interval_s > 0 else 1
    )
    polls = 0
    while status != "ended" and polls < max_polls:
        sleep(poll_interval_s)
        polls += 1
        refreshed = client.messages.batches.retrieve(batch_id)
        status = getattr(refreshed, "processing_status", None)
    return status


def _peek_first_result(client, batch_id: str):
    """First result entry of an ended batch (or None). Streams one row only.

    Used to establish OWNERSHIP: a batch's requests all come from one
    submitter, so one custom_id decides whether the batch is ours
    (`tender-<id>`). It deliberately does NOT try to decide whether the batch
    was already collected — a mid-collect crash leaves a collected PREFIX, so
    any small peek would mistake a partially-collected batch for a fully
    collected one and discard paid work. Re-collection is idempotent and
    unbilled; the in-process `_handled_batch_ids` memo keeps it from
    repeating within a process."""
    try:
        for entry in client.messages.batches.results(batch_id):
            return entry
    except Exception as exc:  # noqa: BLE001 — a peek failure just skips recovery
        logger.warning(
            "classification.backfill_peek_failed",
            batch_id=batch_id,
            error_type=type(exc).__name__,
            error=str(exc)[:500],
        )
    return None


def _batch_is_ours(client, batch_id: str) -> bool:
    """Ownership check for an ENDED batch via the one-row peek."""
    first = _peek_first_result(client, batch_id)
    if first is None:
        return False
    return _tender_id_from_custom_id(getattr(first, "custom_id", "")) is not None


def _scan_recent_batches(client) -> tuple[str | None, list[str]]:
    """List recent batches on the API: (newest in-flight batch id | None,
    [recently-ended batch ids]). The real SDK's list() AUTO-PAGINATES when
    iterated (`limit` is only a page size), so the index bound and the
    newest-first created_at window break are what stop this walking the
    org's entire batch history."""
    list_fn = getattr(client.messages.batches, "list", None)
    if list_fn is None:  # very old SDK / minimal test double — skip self-heal
        logger.debug("classification.backfill_recovery_unavailable")
        return None, []
    inflight_id: str | None = None
    ended_ids: list[str] = []
    now = datetime.now(UTC)
    for index, batch in enumerate(list_fn(limit=RECOVERY_SCAN_LIMIT)):
        if index >= RECOVERY_SCAN_LIMIT:
            break
        created = getattr(batch, "created_at", None)
        if isinstance(created, datetime):
            if created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            if now - created > RECOVERY_WINDOW:
                break
        status = getattr(batch, "processing_status", None)
        if status in ("in_progress", "canceling"):
            if inflight_id is None:
                inflight_id = getattr(batch, "id", None)
        elif status == "ended":
            ended_ids.append(getattr(batch, "id", None))
    return inflight_id, [b for b in ended_ids if b]


def _recover_ended_batches(
    db_factory, client, ended_ids: list[str], summary: BackfillSummary
) -> None:
    """Collect recently-ended batches of OURS that this process hasn't
    handled yet. Ownership comes from the one-row custom_id peek; beyond
    that we ALWAYS collect — a mid-collect crash leaves a collected prefix,
    so no cheap peek can distinguish "fully collected" from "partially
    collected with paid work remaining", and re-collection is idempotent
    (per-row skip-current) and unbilled. The `_handled_batch_ids` memo stops
    the re-stream repeating within a process; a failed collection is NOT
    memoised so the next run retries it."""
    for batch_id in ended_ids:
        if batch_id in _handled_batch_ids:
            continue
        if not _batch_is_ours(client, batch_id):
            _handled_batch_ids.add(batch_id)  # foreign (or unreadable): leave alone
            continue
        collected = collect_results(db_factory, batch_id, client=client)
        _merge_collected(summary, collected, recovered=True)
        if collected.status == "error":
            logger.warning(
                "classification.backfill_recovery_collect_failed",
                batch_id=batch_id,
                error_type=collected.error_type,
                error=collected.error,
                recovered_before_failure=collected.classified,
            )
            continue  # not memoised — retried next run
        _handled_batch_ids.add(batch_id)
        logger.info(
            "classification.backfill_recovered_batch",
            batch_id=batch_id,
            recovered=collected.classified,
            errored=collected.errored,
            skipped_already_current=collected.skipped_already_current,
        )


def run_backfill(
    db_factory=SessionLocal,
    *,
    limit: int,
    client=None,
    chunk_size: int | None = None,
    poll_interval_s: float = 5.0,
    max_wait_s: float = 1800.0,
    sleep=time.sleep,
) -> BackfillSummary:
    """Classify up to `limit` unclassified tenders via the Batch API, in
    sequential chunks, self-healing any state a previously-failed run left
    behind. Safe to re-run (watermark resumable); `max_wait_s` is the polling
    budget PER BATCH, not per run."""
    summary = BackfillSummary()
    if client is None and not settings.anthropic_api_key:
        logger.warning("classification.backfill_no_api_key")
        summary.status = "no_api_key"
        return summary
    client = client or _make_client()
    chunk_size = max(1, chunk_size or settings.classifier_backfill_chunk_size)

    # --- self-heal: adopt/recover what a failed run abandoned ---------------
    # Never create a new batch while one of ours is still in flight — adopt
    # it instead (and harvest any ended strays first). This both avoids
    # stacking batches behind a stuck one and recovers work already paid for.
    inflight_id: str | None = None
    try:
        inflight_id, ended_ids = _scan_recent_batches(client)
        if ended_ids:
            _recover_ended_batches(db_factory, client, ended_ids, summary)
    except Exception as exc:  # noqa: BLE001 — self-heal must not block the run
        logger.warning(
            "classification.backfill_recovery_failed",
            error_type=type(exc).__name__,
            error=str(exc)[:500],
        )

    if inflight_id is not None:
        summary.batch_id = inflight_id
        summary.batch_ids.append(inflight_id)
        logger.info(
            "classification.backfill_adopted_inflight",
            batch_id=inflight_id,
            detail="a previous run's batch is still processing — polling it "
            "instead of submitting a new one",
        )
        try:
            status = _wait_until_ended(
                client, inflight_id, "in_progress",
                poll_interval_s=poll_interval_s, max_wait_s=max_wait_s,
                sleep=sleep,
            )
        except Exception as exc:  # noqa: BLE001
            return _fail(summary, "adopt", exc, adopted_batch_id=inflight_id)
        if status != "ended":
            summary.status = "pending"
            logger.warning(
                "classification.backfill_pending",
                batch_id=inflight_id,
                batch_status=status,
                adopted=True,
                detail="adopted batch still processing — re-run later; "
                "the next run re-adopts it",
            )
            return summary
        # Ownership is only checkable now that it ended: a batch from another
        # workload sharing the key must not be collected (its custom_ids
        # could collide with `tender-<int>` and stamp the wrong tenders).
        if _batch_is_ours(client, inflight_id):
            collected = collect_results(db_factory, inflight_id, client=client)
            _merge_collected(summary, collected, recovered=True)
            if collected.status == "error":
                return _fail(
                    summary, "collect",
                    error_type=collected.error_type, error=collected.error,
                    adopted_batch_id=inflight_id,
                    recovered_before_failure=collected.classified,
                )
            _handled_batch_ids.add(inflight_id)
            logger.info(
                "classification.backfill_recovered_batch",
                batch_id=inflight_id,
                recovered=collected.classified,
                errored=collected.errored,
                skipped_already_current=collected.skipped_already_current,
            )
        else:
            _handled_batch_ids.add(inflight_id)
            logger.warning(
                "classification.backfill_adopted_foreign",
                batch_id=inflight_id,
                detail="adopted batch's results are not ours (custom_id "
                "scheme mismatch) — skipped; proceeding to submission",
            )

    # --- chunked submission over what's still pending ------------------------
    # `blocked` tracks ids from chunks that didn't fully classify (errored /
    # skipped rows): they stay on the pending watermark, so without the
    # filter every later chunk of THIS run would re-select and RE-BUY them.
    # They're left for a later run / the worker instead.
    blocked: set[int] = set()
    remaining = limit
    chunk_index = 0
    while remaining > 0:
        request_count = min(chunk_size, remaining)
        chunk_index += 1
        try:
            with db_factory() as db:
                pending = pending_classification(
                    db, request_count + len(blocked)
                )
                fresh = [t for t in pending if t.id not in blocked][
                    :request_count
                ]
                requests = [build_batch_request(t) for t in fresh]
                tender_ids = [t.id for t in fresh]
        except Exception as exc:  # noqa: BLE001
            return _fail(summary, "select", exc, chunk=chunk_index)

        if not requests:
            break  # nothing fresh left within the limit window

        try:
            batch = client.messages.batches.create(requests=requests)
        except Exception as exc:  # noqa: BLE001
            return _fail(
                summary, "submit", exc,
                chunk=chunk_index, selected=len(requests),
            )
        batch_id = getattr(batch, "id", None)
        summary.batch_id = batch_id
        summary.batch_ids.append(batch_id)
        summary.submitted += len(requests)
        summary.chunks_submitted += 1
        logger.info(
            "classification.backfill_chunk_submitted",
            batch_id=batch_id,
            chunk=chunk_index,
            count=len(requests),
            tender_ids=tender_ids[:20],
        )

        try:
            status = _wait_until_ended(
                client, batch_id, getattr(batch, "processing_status", None),
                poll_interval_s=poll_interval_s, max_wait_s=max_wait_s,
                sleep=sleep,
            )
        except Exception as exc:  # noqa: BLE001
            # The batch keeps processing on the API; the next run adopts it
            # via the self-heal scan, so nothing is lost or blocked.
            return _fail(
                summary, "poll", exc, chunk=chunk_index, poll_batch_id=batch_id
            )

        if status != "ended":
            summary.status = "pending"
            logger.warning(
                "classification.backfill_pending",
                batch_id=batch_id,
                batch_status=status,
                chunk=chunk_index,
                detail="chunk still processing past max_wait — re-run later; "
                "the next run adopts it via self-heal",
            )
            return summary

        try:
            collected = collect_results(db_factory, batch_id, client=client)
        except Exception as exc:  # noqa: BLE001 — belt-and-braces; collect_results guards itself
            return _fail(
                summary, "collect", exc,
                chunk=chunk_index, collect_batch_id=batch_id,
            )
        _merge_collected(summary, collected, recovered=False)
        if collected.status == "error":
            # Partial counts are already merged (committed rows are real);
            # the ended batch stays un-memoised so the next run's self-heal
            # collects the rest.
            return _fail(
                summary, "collect",
                error_type=collected.error_type, error=collected.error,
                chunk=chunk_index, collect_batch_id=batch_id,
                classified_before_failure=collected.classified,
            )
        _handled_batch_ids.add(batch_id)
        summary.chunks_completed += 1
        logger.info(
            "classification.backfill_chunk_complete",
            batch_id=batch_id,
            chunk=chunk_index,
            classified=collected.classified,
            errored=collected.errored,
            skipped_already_current=collected.skipped_already_current,
        )

        if collected.classified < len(requests):
            # Rows that didn't classify (errored / skipped) stay pending —
            # exclude them from this run's later chunks (no double-buying).
            # Over-blocking the classified ones is harmless: the watermark
            # already excludes them from selection.
            blocked.update(tender_ids)

        if (
            collected.classified == 0
            and collected.errored == 0
            and collected.skipped_already_current == 0
            and collected.skipped_no_tender == 0
        ):
            # The batch produced literally nothing for our rows — an API
            # anomaly; stop rather than buy the same silence again. (An
            # all-errored or worker-classified chunk is NOT this case: those
            # continue — `blocked` / the watermark prevent re-buying.)
            logger.warning(
                "classification.backfill_no_progress",
                batch_id=batch_id,
                chunk=chunk_index,
            )
            break

        remaining -= len(requests)
        if len(requests) < request_count:
            break  # pool drained mid-chunk

    if (
        summary.submitted == 0
        and summary.recovered == 0
        and summary.classified == 0
    ):
        summary.status = "empty"
        logger.info("classification.backfill_empty")
        return summary

    logger.info(
        "classification.backfill_complete",
        submitted=summary.submitted,
        classified=summary.classified,
        recovered=summary.recovered,
        errored=summary.errored,
        skipped_already_current=summary.skipped_already_current,
        chunks_submitted=summary.chunks_submitted,
        chunks_completed=summary.chunks_completed,
        batch_ids=summary.batch_ids,
        input_tokens=summary.input_tokens,
        output_tokens=summary.output_tokens,
    )
    return summary
