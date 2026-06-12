"""Classification backfill tests (Phase 3a, hardened rev 2) — the Batch-API
path.

Offline: a fake batch client stands in for `client.messages.batches`
(list / create / retrieve / results), modelled on the REAL SDK semantics:
create() returns processing_status "in_progress" (never "ended"), list()
auto-paginates when iterated (`limit` is only a page size), results() is a
stream that can die mid-flight. Pins the post-incident contract (2026-06-12:
a died 10k run abandoned its batch and every subsequent run failed within
seconds, with the actual exception invisible):

  - chunked submission for large limits, sequential, polled per chunk,
    per-row commit;
  - the instant-failure-after-failed-run scenario is RECOVERABLE: a new run
    adopts the abandoned in-flight batch instead of creating blindly, and
    harvests its results;
  - ended-but-uncollected batches — including PARTIALLY-collected ones from
    a mid-collect crash — are recovered (paid work not re-bought);
  - per-phase failures (select/submit/poll/collect/adopt) return structured
    summaries and log the exception type/message as plain fields;
  - rows that fail once in a run are never re-bought by later chunks of the
    same run; bounded scan windows; one-run-at-a-time admin lock.
"""
from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from structlog.testing import capture_logs

from tender_agent.models import Tender
from tender_agent.services.classification import backfill as backfill_module
from tender_agent.services.classification.backfill import (
    RECOVERY_SCAN_LIMIT,
    run_backfill,
)
from tender_agent.services.classification.taxonomy import CLASSIFIER_VERSION
from tests._billing_fixtures import make_engine_and_session

NOW = datetime(2026, 6, 12, 12, 0, tzinfo=UTC)

_CONSTRUCTION = (
    '{"primary_sector": "Construction & Built Environment",'
    ' "secondary_sectors": [], '
    '"construction_subcategories": ["Refurbishment & renovation"]}'
)


@pytest.fixture(autouse=True)
def _clear_handled_batches():
    """The in-process collected-batches memo must not leak between tests."""
    backfill_module._handled_batch_ids.clear()
    yield
    backfill_module._handled_batch_ids.clear()


def _noop_sleep(_seconds) -> None:
    return None


# --- fake Batch API ----------------------------------------------------------


def _succeeded_entry(custom_id: str, text: str):
    message = SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=8, output_tokens=4),
    )
    return SimpleNamespace(
        custom_id=custom_id,
        result=SimpleNamespace(type="succeeded", message=message),
    )


def _errored_entry(custom_id: str):
    return SimpleNamespace(
        custom_id=custom_id,
        result=SimpleNamespace(type="errored", message=None),
    )


def _listed_batch(batch_id: str, status: str, *, age: timedelta | None = None):
    return SimpleNamespace(
        id=batch_id,
        processing_status=status,
        created_at=datetime.now(UTC) - (age or timedelta(minutes=30)),
    )


class _FakeBatches:
    """Configurable stand-in for `client.messages.batches`, mirroring the
    real SDK where it matters:

    - create() returns processing_status "in_progress" (a real batch can
      never be "ended" at create time); a created batch defaults to the
      progression ["in_progress", "ended"], each retrieve() advancing one
      step. Pass `status_sequences` to override per batch id.
    - list() returns an iterator over ALL `listed` entries regardless of
      `limit` — the real SDK auto-paginates on iteration; `list_yields`
      counts how many entries the scanner actually consumed.
    - results() is a generator (a stream): `fail_results_for` raises at the
      first row, `fail_results_after[batch_id]=k` raises after k rows.
    - `queue_blocked_by`: while that batch id is not "ended", create()
      raises (models a create-rejection while another batch is in flight).
    - `on_retrieve(batch_id)`: test hook fired on every retrieve — used to
      simulate the scheduler worker classifying rows during a poll window.
    Counters: create_calls, retrieve_calls, results_calls, list_yields.
    """

    def __init__(
        self,
        *,
        response_fn=None,
        listed=(),
        preset_results=None,
        status_sequences=None,
        queue_blocked_by=None,
        fail_create_on_call=None,
        fail_retrieve=False,
        fail_results_for=(),
        fail_results_after=None,
        on_retrieve=None,
    ) -> None:
        self.response_fn = response_fn or (lambda _cid: _CONSTRUCTION)
        self.listed = list(listed)
        self.preset_results = dict(preset_results or {})
        self.status_sequences = {
            k: list(v) for k, v in (status_sequences or {}).items()
        }
        self.queue_blocked_by = queue_blocked_by
        self.fail_create_on_call = fail_create_on_call
        self.fail_retrieve = fail_retrieve
        self.fail_results_for = set(fail_results_for)
        self.fail_results_after = dict(fail_results_after or {})
        self.on_retrieve = on_retrieve
        self.created: list[tuple[str, list[dict]]] = []
        self.create_calls = 0
        self.retrieve_calls: dict[str, int] = {}
        self.results_calls: dict[str, int] = {}
        self.list_yields = 0
        self._status: dict[str, str] = {
            b.id: b.processing_status for b in self.listed
        }

    def list(self, limit=20):
        del limit  # the real SDK auto-paginates past it on iteration

        def _generate():
            for batch in self.listed:
                self.list_yields += 1
                yield batch

        return _generate()

    def create(self, *, requests):
        self.create_calls += 1
        if (
            self.queue_blocked_by
            and self._status.get(self.queue_blocked_by) != "ended"
        ):
            raise RuntimeError(
                "rate_limit_error: batch create rejected while another batch "
                "is in flight"
            )
        if self.fail_create_on_call == self.create_calls:
            raise RuntimeError("boom on create")
        batch_id = f"batch_{self.create_calls}"
        self.created.append((batch_id, list(requests)))
        seq = self.status_sequences.setdefault(
            batch_id, ["in_progress", "ended"]
        )
        self._status[batch_id] = seq[0]
        return SimpleNamespace(id=batch_id, processing_status=seq[0])

    def retrieve(self, batch_id):
        self.retrieve_calls[batch_id] = self.retrieve_calls.get(batch_id, 0) + 1
        if self.fail_retrieve:
            raise RuntimeError("boom on retrieve")
        if self.on_retrieve is not None:
            self.on_retrieve(batch_id)
        seq = self.status_sequences.get(batch_id)
        if seq and len(seq) > 1:
            seq.pop(0)
        if seq:
            self._status[batch_id] = seq[0]
        return SimpleNamespace(
            id=batch_id,
            processing_status=self._status.get(batch_id, "ended"),
        )

    def _entries_for(self, batch_id):
        if batch_id in self.preset_results:
            return list(self.preset_results[batch_id])
        entries = []
        for created_id, requests in self.created:
            if created_id != batch_id:
                continue
            for request in requests:
                custom_id = request["custom_id"]
                text = self.response_fn(custom_id)
                entries.append(
                    _errored_entry(custom_id)
                    if text is None
                    else _succeeded_entry(custom_id, text)
                )
        return entries

    def results(self, batch_id):
        self.results_calls[batch_id] = self.results_calls.get(batch_id, 0) + 1
        if batch_id in self.fail_results_for:
            raise RuntimeError("boom on results stream")
        fail_after = self.fail_results_after.get(batch_id)
        for index, entry in enumerate(self._entries_for(batch_id)):
            if fail_after is not None and index >= fail_after:
                raise RuntimeError("results stream died mid-flight")
            yield entry


class _FakeBatchClient:
    def __init__(self, **kwargs) -> None:
        self.messages = SimpleNamespace(batches=_FakeBatches(**kwargs))

    @property
    def batches(self) -> _FakeBatches:
        return self.messages.batches


def _seed_tenders(factory, n: int) -> list[int]:
    ids: list[int] = []
    with factory() as db:
        for i in range(n):
            tender = Tender(
                source_code="FTS",
                source_ref=f"t{i}",
                title=f"Tender {i}",
                first_seen_at=NOW,
                last_seen_at=NOW,
            )
            db.add(tender)
            db.commit()
            db.refresh(tender)
            ids.append(tender.id)
    return ids


def _mark_current(factory, tender_ids: list[int]) -> None:
    with factory() as db:
        for tid in tender_ids:
            tender = db.get(Tender, tid)
            tender.primary_sector = "IT & Digital"
            tender.classifier_version = CLASSIFIER_VERSION
        db.commit()


def _run(factory, client, *, limit, **kwargs):
    return run_backfill(
        factory,
        limit=limit,
        client=client,
        poll_interval_s=0,
        sleep=_noop_sleep,
        **kwargs,
    )


def _unclassified_count(factory) -> int:
    with factory() as db:
        return db.query(Tender).filter(Tender.primary_sector.is_(None)).count()


# --- the original contract (still holds) -------------------------------------


def test_backfill_classifies_pool_and_logs_tokens() -> None:
    _engine, factory = make_engine_and_session()
    _seed_tenders(factory, 3)
    client = _FakeBatchClient()

    summary = _run(factory, client, limit=10)

    assert summary.status == "ok"
    assert summary.submitted == 3
    assert summary.classified == 3
    assert summary.input_tokens == 24 and summary.output_tokens == 12
    with factory() as db:
        rows = db.query(Tender).all()
        assert {t.primary_sector for t in rows} == {
            "Construction & Built Environment"
        }
        assert all(t.classifier_version == CLASSIFIER_VERSION for t in rows)


def test_backfill_is_bounded_by_limit() -> None:
    _engine, factory = make_engine_and_session()
    _seed_tenders(factory, 5)

    summary = _run(factory, _FakeBatchClient(), limit=2)

    assert summary.submitted == 2
    assert summary.classified == 2
    assert _unclassified_count(factory) == 3


def test_backfill_is_idempotent_and_resumable() -> None:
    _engine, factory = make_engine_and_session()
    _seed_tenders(factory, 3)

    _run(factory, _FakeBatchClient(), limit=10)
    again = _run(factory, _FakeBatchClient(), limit=10)

    assert again.status == "empty"
    assert again.submitted == 0


def test_backfill_pending_batch_writes_nothing_and_reports_batch_id() -> None:
    _engine, factory = make_engine_and_session()
    _seed_tenders(factory, 2)
    client = _FakeBatchClient(
        status_sequences={"batch_1": ["in_progress"]}  # never ends
    )

    summary = _run(factory, client, limit=10, max_wait_s=0.005)

    assert summary.status == "pending"
    assert summary.batch_id == "batch_1"  # the next run adopts it by id
    assert summary.submitted == 2
    assert summary.classified == 0
    assert _unclassified_count(factory) == 2


def test_backfill_errored_result_skips_that_tender_only() -> None:
    _engine, factory = make_engine_and_session()
    ids = _seed_tenders(factory, 3)
    bad = f"tender-{ids[1]}"
    client = _FakeBatchClient(
        response_fn=lambda cid: None if cid == bad else _CONSTRUCTION
    )

    summary = _run(factory, client, limit=3)

    assert summary.submitted == 3
    assert summary.classified == 2
    assert summary.errored == 1
    with factory() as db:
        assert db.get(Tender, ids[1]).primary_sector is None
        assert db.get(Tender, ids[0]).primary_sector == (
            "Construction & Built Environment"
        )


# --- chunking (rev 2) ---------------------------------------------------------


def test_large_limit_is_chunked_into_sequential_batches() -> None:
    """A big limit never lands in one giant create: it's split into chunks of
    `chunk_size`, each its own batch, POLLED to completion (the fake mirrors
    the real API: create returns in_progress), then collected, sequentially."""
    _engine, factory = make_engine_and_session()
    _seed_tenders(factory, 7)
    client = _FakeBatchClient()

    summary = _run(factory, client, limit=7, chunk_size=3)

    assert summary.status == "ok"
    assert summary.chunks_submitted == 3
    assert summary.chunks_completed == 3
    assert [len(reqs) for _id, reqs in client.batches.created] == [3, 3, 1]
    # Each chunk was actually polled to completion, not assumed ended.
    assert all(
        client.batches.retrieve_calls.get(batch_id, 0) >= 1
        for batch_id, _reqs in client.batches.created
    )
    assert summary.classified == 7
    assert _unclassified_count(factory) == 0


def test_chunk_failure_keeps_earlier_chunks_progress() -> None:
    """Per-chunk commit: a submit failure on chunk 2 loses NOTHING from chunk
    1, and the summary carries the structured failure (phase/error fields)."""
    _engine, factory = make_engine_and_session()
    _seed_tenders(factory, 6)
    client = _FakeBatchClient(fail_create_on_call=2)

    with capture_logs() as logs:
        summary = _run(factory, client, limit=6, chunk_size=3)

    assert summary.status == "error"
    assert summary.phase == "submit"
    assert summary.error_type == "RuntimeError"
    assert "boom on create" in (summary.error or "")
    assert summary.chunks_completed == 1
    assert summary.classified == 3
    # Chunk 1's three tenders survived the chunk-2 failure.
    assert _unclassified_count(factory) == 3

    failed = [e for e in logs if e["event"] == "classification.backfill_failed"]
    assert failed, "no backfill_failed event emitted"
    event = failed[0]
    assert event["error_type"] == "RuntimeError"
    assert "boom on create" in event["error"]
    assert event["phase"] == "submit"
    assert event["selected"] == 3
    assert event["chunk"] == 2


def test_errored_rows_are_bought_once_and_run_continues() -> None:
    """Rows whose results error are NEVER re-submitted by a later chunk of
    the same run (no double-buying), and the run continues through the limit
    so fresh rows are still reached."""
    _engine, factory = make_engine_and_session()
    ids = _seed_tenders(factory, 4)
    bad = f"tender-{ids[0]}"
    client = _FakeBatchClient(
        response_fn=lambda cid: None if cid == bad else _CONSTRUCTION
    )

    summary = _run(factory, client, limit=4, chunk_size=2)

    # Every tender id appears in EXACTLY one create call.
    submitted_ids = [
        req["custom_id"]
        for _bid, reqs in client.batches.created
        for req in reqs
    ]
    assert len(submitted_ids) == len(set(submitted_ids))
    # The last tender was reached despite the errored head row.
    assert f"tender-{ids[3]}" in submitted_ids
    assert summary.submitted == 4
    assert summary.classified == 3
    assert summary.errored == 1
    with factory() as db:
        assert db.get(Tender, ids[0]).primary_sector is None


def test_all_errored_run_terminates_with_each_row_bought_once() -> None:
    """Even when EVERY result errors, the run terminates (bounded by limit)
    and no row is bought twice; the rows stay on the watermark for later."""
    _engine, factory = make_engine_and_session()
    _seed_tenders(factory, 5)
    client = _FakeBatchClient(response_fn=lambda _cid: None)

    summary = _run(factory, client, limit=5, chunk_size=2)

    submitted_ids = [
        req["custom_id"]
        for _bid, reqs in client.batches.created
        for req in reqs
    ]
    assert len(submitted_ids) == 5
    assert len(set(submitted_ids)) == 5  # each row exactly once
    assert summary.classified == 0
    assert summary.errored == 5
    assert _unclassified_count(factory) == 5


def test_empty_result_stream_stops_with_no_progress() -> None:
    """A batch that produces literally nothing for our rows is an API
    anomaly: stop rather than buy the same silence again."""
    _engine, factory = make_engine_and_session()
    _seed_tenders(factory, 3)
    client = _FakeBatchClient(preset_results={"batch_1": []})

    with capture_logs() as logs:
        summary = _run(factory, client, limit=3, chunk_size=1)

    assert client.batches.create_calls == 1  # stopped after the silent chunk
    assert summary.classified == 0
    assert any(
        e["event"] == "classification.backfill_no_progress" for e in logs
    )


def test_worker_overlap_chunk_continues_not_stops() -> None:
    """If the scheduler's classification worker classifies a chunk's rows
    during the poll window, the chunk collects as skipped_already_current —
    that is NOT a stall (those rows won't be re-selected): the run must
    continue to the next chunk."""
    _engine, factory = make_engine_and_session()
    ids = _seed_tenders(factory, 4)

    def _worker_races_in(batch_id: str) -> None:
        if batch_id == "batch_1":
            _mark_current(factory, ids[:2])  # the worker classified chunk 1

    client = _FakeBatchClient(on_retrieve=_worker_races_in)

    summary = _run(factory, client, limit=4, chunk_size=2)

    assert client.batches.create_calls == 2  # continued to chunk 2
    assert summary.skipped_already_current == 2
    assert summary.classified == 2  # chunk 2's rows
    assert summary.status == "ok"
    assert _unclassified_count(factory) == 0


# --- per-phase structured failures + recoverability ---------------------------


def test_poll_failure_is_structured_and_batch_recoverable_next_run() -> None:
    """A retrieve raise mid-poll (the likeliest shape of the original 40-min
    failure) returns a structured error naming the batch — and that batch is
    then ADOPTED by the next run instead of blocking it."""
    _engine, factory = make_engine_and_session()
    ids = _seed_tenders(factory, 2)

    failing = _FakeBatchClient(
        status_sequences={"batch_1": ["in_progress"]}, fail_retrieve=True
    )
    summary = _run(factory, failing, limit=10)
    assert summary.status == "error"
    assert summary.phase == "poll"
    assert summary.error_type == "RuntimeError"
    assert _unclassified_count(factory) == 2

    # Run 2 (fresh client, the API a while later): the abandoned batch shows
    # up in list() as in_progress then ends; its results are preset; creates
    # are rejected while it is in flight.
    recovering = _FakeBatchClient(
        listed=[_listed_batch("batch_1", "in_progress")],
        status_sequences={"batch_1": ["in_progress", "ended"]},
        preset_results={
            "batch_1": [
                _succeeded_entry(f"tender-{tid}", _CONSTRUCTION) for tid in ids
            ]
        },
        queue_blocked_by="batch_1",
    )
    recovered = _run(factory, recovering, limit=10)

    assert recovered.status == "ok"
    assert recovered.recovered == 2
    assert _unclassified_count(factory) == 0


def test_collect_failure_is_structured_and_batch_recoverable_next_run() -> None:
    """A results-stream failure right after a chunk ends (money already
    spent) is a structured `collect` failure naming the batch — and that
    ENDED batch is then harvested by the next run's self-heal."""
    _engine, factory = make_engine_and_session()
    ids = _seed_tenders(factory, 2)

    failing = _FakeBatchClient(fail_results_for={"batch_1"})
    with capture_logs() as logs:
        summary = _run(factory, failing, limit=10)

    assert summary.status == "error"
    assert summary.phase == "collect"
    assert summary.error_type == "RuntimeError"
    assert summary.batch_id == "batch_1"
    assert _unclassified_count(factory) == 2
    failed = [e for e in logs if e["event"] == "classification.backfill_failed"]
    assert failed and failed[0]["collect_batch_id"] == "batch_1"
    assert failed[0]["chunk"] == 1

    recovering = _FakeBatchClient(
        listed=[_listed_batch("batch_1", "ended")],
        preset_results={
            "batch_1": [
                _succeeded_entry(f"tender-{tid}", _CONSTRUCTION) for tid in ids
            ]
        },
    )
    recovered = _run(factory, recovering, limit=10)

    assert recovered.status == "ok"
    assert recovered.recovered == 2
    assert _unclassified_count(factory) == 0


def test_partial_collect_counts_survive_stream_failure() -> None:
    """A stream that dies mid-collect keeps both the committed rows AND the
    counters for them — the summary never under-reports committed work."""
    _engine, factory = make_engine_and_session()
    _seed_tenders(factory, 3)
    client = _FakeBatchClient(fail_results_after={"batch_1": 1})

    summary = _run(factory, client, limit=3)

    assert summary.status == "error"
    assert summary.phase == "collect"
    assert summary.classified == 1  # the row committed before the stream died
    with factory() as db:
        assert (
            db.query(Tender).filter(Tender.primary_sector.isnot(None)).count()
            == 1
        )


def test_adopt_failure_is_structured() -> None:
    """A retrieve raise while adopting an abandoned in-flight batch is a
    structured `adopt` failure naming the batch, and nothing is created
    behind it."""
    _engine, factory = make_engine_and_session()
    _seed_tenders(factory, 2)
    client = _FakeBatchClient(
        listed=[_listed_batch("batch_big", "in_progress")],
        fail_retrieve=True,
        queue_blocked_by="batch_big",
    )

    with capture_logs() as logs:
        summary = _run(factory, client, limit=10)

    assert summary.status == "error"
    assert summary.phase == "adopt"
    assert summary.error_type == "RuntimeError"
    assert client.batches.create_calls == 0
    failed = [e for e in logs if e["event"] == "classification.backfill_failed"]
    assert failed and failed[0]["adopted_batch_id"] == "batch_big"


def test_select_failure_is_structured(monkeypatch) -> None:
    """A selection-phase raise is a structured `select` failure."""
    _engine, factory = make_engine_and_session()
    _seed_tenders(factory, 2)

    def _boom(_db, _limit):
        raise RuntimeError("select exploded")

    monkeypatch.setattr(backfill_module, "pending_classification", _boom)

    summary = _run(factory, _FakeBatchClient(), limit=10)

    assert summary.status == "error"
    assert summary.phase == "select"
    assert "select exploded" in (summary.error or "")


def test_poisoned_row_is_contained_and_rest_of_batch_commits(monkeypatch) -> None:
    """The per-row guard in collect_results: one row whose write-back raises
    is rolled back and logged (backfill_row_failed, plain error fields);
    every other row in the same batch still commits."""
    from tender_agent.services.classification.classifier import (
        apply_classification as real_apply,
    )

    _engine, factory = make_engine_and_session()
    ids = _seed_tenders(factory, 3)
    poisoned = ids[1]

    def _apply_or_boom(tender, parsed):
        if tender.id == poisoned:
            raise RuntimeError("poisoned row")
        real_apply(tender, parsed)

    monkeypatch.setattr(backfill_module, "apply_classification", _apply_or_boom)

    with capture_logs() as logs:
        summary = _run(factory, _FakeBatchClient(), limit=3)

    assert summary.status == "ok"
    assert summary.classified == 2
    assert summary.errored == 1
    with factory() as db:
        assert db.get(Tender, poisoned).primary_sector is None
        assert db.get(Tender, ids[0]).primary_sector == (
            "Construction & Built Environment"
        )
        assert db.get(Tender, ids[2]).primary_sector == (
            "Construction & Built Environment"
        )
    row_failed = [
        e for e in logs if e["event"] == "classification.backfill_row_failed"
    ]
    assert row_failed
    assert row_failed[0]["tender_id"] == poisoned
    assert row_failed[0]["error_type"] == "RuntimeError"
    assert "poisoned row" in row_failed[0]["error"]


# --- the incident scenario: instant failure after a failed big run ------------


def test_instant_failure_after_failed_run_is_self_healed() -> None:
    """THE incident regression. An abandoned in-flight batch exists and
    creates are being rejected while it is in flight. The run must NOT
    create blindly: it adopts the in-flight batch, harvests its results
    (work already paid for), and only then submits the remainder — by which
    point creates succeed again. No instant failure, no double-spend."""
    _engine, factory = make_engine_and_session()
    ids = _seed_tenders(factory, 3)
    covered = ids[:2]  # the abandoned batch holds results for these two

    client = _FakeBatchClient(
        listed=[_listed_batch("batch_big", "in_progress")],
        status_sequences={"batch_big": ["in_progress", "ended"]},
        preset_results={
            "batch_big": [
                _succeeded_entry(f"tender-{tid}", _CONSTRUCTION)
                for tid in covered
            ]
        },
        queue_blocked_by="batch_big",
    )

    summary = _run(factory, client, limit=10)

    assert summary.status == "ok"
    assert summary.recovered == 2  # the abandoned batch's paid work harvested
    assert summary.submitted == 1  # only the genuinely-uncovered tender
    assert summary.classified == 1
    # The remainder was submitted AFTER adoption — exactly one create, and it
    # did not raise.
    assert client.batches.create_calls == 1
    assert _unclassified_count(factory) == 0


def test_adopted_batch_still_processing_returns_pending_not_error() -> None:
    """If the adopted batch outlives the wait budget, the run reports
    `pending` with the batch id and submits nothing new (no pile-up of our
    batches behind a stuck one)."""
    _engine, factory = make_engine_and_session()
    _seed_tenders(factory, 2)
    client = _FakeBatchClient(
        listed=[_listed_batch("batch_big", "in_progress")],
        status_sequences={"batch_big": ["in_progress", "in_progress"]},
        queue_blocked_by="batch_big",
    )

    summary = _run(factory, client, limit=10, max_wait_s=0.005)

    assert summary.status == "pending"
    assert summary.batch_id == "batch_big"
    assert client.batches.create_calls == 0


def test_canceling_batch_is_adopted_not_created_over() -> None:
    """An operator-cancelled batch sits in 'canceling' before it ends. It
    must be treated as in-flight and ADOPTED — creating over it would hit
    the same rejection the incident produced."""
    _engine, factory = make_engine_and_session()
    ids = _seed_tenders(factory, 2)
    client = _FakeBatchClient(
        listed=[_listed_batch("batch_big", "canceling")],
        status_sequences={"batch_big": ["canceling", "ended"]},
        preset_results={"batch_big": [_errored_entry(f"tender-{ids[0]}")]},
        queue_blocked_by="batch_big",
    )

    summary = _run(factory, client, limit=10)

    # Adopted (waited out), not created over: the single create happened only
    # AFTER the canceling batch ended — and did not raise.
    assert summary.status == "ok"
    assert client.batches.create_calls == 1
    assert summary.batch_ids[0] == "batch_big"
    assert summary.submitted == 2  # cancelled rows re-bought normally
    assert _unclassified_count(factory) == 0


def test_adopting_foreign_inflight_batch_is_safe_and_run_proceeds() -> None:
    """Ownership of an IN-FLIGHT batch cannot be verified (custom_ids only
    become visible after it ends), so the scan adopts any in_progress batch.
    Pin the blast radius: adopting a FOREIGN batch costs at most one wait
    budget — once it ends the ownership peek skips its results entirely, no
    tender data is touched, and normal submission still happens."""
    _engine, factory = make_engine_and_session()
    _seed_tenders(factory, 2)
    client = _FakeBatchClient(
        listed=[_listed_batch("batch_alien", "in_progress")],
        status_sequences={"batch_alien": ["in_progress", "ended"]},
        preset_results={
            "batch_alien": [
                _succeeded_entry("other-app-1", '{"not": "our schema"}'),
                _succeeded_entry("other-app-2", "free text"),
            ]
        },
    )

    summary = _run(factory, client, limit=10)

    assert summary.status == "ok"
    assert summary.recovered == 0  # nothing harvested from the alien batch
    assert summary.submitted == 2  # our pool still submitted normally
    assert summary.classified == 2
    assert client.batches.create_calls == 1
    # The alien batch was peeked once for ownership — never fully collected.
    assert client.batches.results_calls.get("batch_alien") == 1
    assert _unclassified_count(factory) == 0


# --- ended-batch recovery: ownership, partial collects, memoisation, bounds ---


def test_ended_uncollected_batch_is_recovered_before_submitting() -> None:
    """A batch that ENDED after its run died is harvested by the self-heal
    scan (ours by custom_id), and only the remainder is bought."""
    _engine, factory = make_engine_and_session()
    ids = _seed_tenders(factory, 3)
    covered = ids[:2]
    client = _FakeBatchClient(
        listed=[_listed_batch("batch_old", "ended")],
        preset_results={
            "batch_old": [
                _succeeded_entry(f"tender-{tid}", _CONSTRUCTION)
                for tid in covered
            ]
        },
    )

    summary = _run(factory, client, limit=10)

    assert summary.status == "ok"
    assert summary.recovered == 2
    assert summary.submitted == 1
    assert client.batches.create_calls == 1
    assert _unclassified_count(factory) == 0


def test_partially_collected_batch_remainder_is_recovered() -> None:
    """A mid-collect crash leaves a collected PREFIX. The recovery must NOT
    judge the batch by its first row (which looks collected) — it always
    re-collects our ended batches, so the paid remainder is harvested
    instead of re-bought. Foreign batches stay untouched (peeked once), and
    the in-process memo stops the re-stream repeating."""
    _engine, factory = make_engine_and_session()
    ids = _seed_tenders(factory, 3)
    _mark_current(factory, ids[:1])  # the collected prefix from the crash

    client = _FakeBatchClient(
        listed=[
            _listed_batch("batch_foreign", "ended"),
            _listed_batch("batch_partial", "ended"),
        ],
        preset_results={
            "batch_foreign": [_succeeded_entry("other-app-1", _CONSTRUCTION)],
            "batch_partial": [
                _succeeded_entry(f"tender-{ids[0]}", _CONSTRUCTION),
                _succeeded_entry(f"tender-{ids[1]}", _CONSTRUCTION),
            ],
        },
    )

    summary = _run(factory, client, limit=10)

    assert summary.recovered == 1  # the paid remainder (ids[1]) harvested
    assert summary.skipped_already_current == 1  # the prefix row, idempotent
    assert summary.submitted == 1  # only ids[2] bought
    # Foreign batch: ownership peek only. Ours: peek + one full collection.
    # (batch_1, the run's own chunk, also appears with its one collection.)
    assert client.batches.results_calls["batch_foreign"] == 1
    assert client.batches.results_calls["batch_partial"] == 2
    with factory() as db:
        # The foreign batch's row never overwrote the prefix tender.
        assert db.get(Tender, ids[0]).primary_sector == "IT & Digital"

    # Second run, same process: both batches are memoised — no re-peek, no
    # re-stream, and nothing left to submit.
    again = _run(factory, client, limit=10)
    assert again.status == "empty"
    assert client.batches.results_calls["batch_foreign"] == 1
    assert client.batches.results_calls["batch_partial"] == 2


def test_recovery_window_cutoff_stops_the_scan() -> None:
    """The list is newest-first; the scan stops at the first batch older
    than RECOVERY_WINDOW — ancient batches are never peeked or recovered."""
    _engine, factory = make_engine_and_session()
    ids = _seed_tenders(factory, 2)
    client = _FakeBatchClient(
        listed=[
            _listed_batch("batch_fresh", "ended"),
            _listed_batch("batch_ancient", "ended", age=timedelta(hours=72)),
            _listed_batch("batch_tail", "ended", age=timedelta(hours=73)),
        ],
        preset_results={
            "batch_fresh": [_succeeded_entry(f"tender-{ids[0]}", _CONSTRUCTION)],
            "batch_ancient": [
                _succeeded_entry(f"tender-{ids[1]}", _CONSTRUCTION)
            ],
        },
    )

    summary = _run(factory, client, limit=0)  # recovery only, no submission

    assert summary.recovered == 1  # batch_fresh harvested
    assert "batch_ancient" not in client.batches.results_calls
    assert "batch_tail" not in client.batches.results_calls
    # Iteration stopped AT the ancient batch — the tail was never consumed.
    assert client.batches.list_yields == 2
    with factory() as db:
        assert db.get(Tender, ids[1]).primary_sector is None


def test_recovery_scan_limit_bounds_auto_pagination() -> None:
    """The real SDK auto-paginates list() past `limit`; the scan's index
    bound is what stops it walking the org's whole batch history."""
    _engine, factory = make_engine_and_session()
    listed = [
        _listed_batch(f"batch_f{i}", "ended")
        for i in range(RECOVERY_SCAN_LIMIT + 5)
    ]
    client = _FakeBatchClient(listed=listed)  # no preset → peeks find nothing

    summary = _run(factory, client, limit=0)

    assert summary.status == "empty"
    assert client.batches.list_yields <= RECOVERY_SCAN_LIMIT + 1
    # Nothing beyond the scan limit was ever peeked.
    peeked = set(client.batches.results_calls)
    assert all(
        int(batch_id.removeprefix("batch_f")) < RECOVERY_SCAN_LIMIT
        for batch_id in peeked
    )


def test_recovery_scan_failure_does_not_block_the_run() -> None:
    """Self-heal is best-effort: a list() blow-up is logged with plain error
    fields and the run proceeds to normal submission."""
    _engine, factory = make_engine_and_session()
    _seed_tenders(factory, 2)
    client = _FakeBatchClient()

    def _exploding_list(limit=20):
        raise RuntimeError("list unavailable")

    client.batches.list = _exploding_list

    with capture_logs() as logs:
        summary = _run(factory, client, limit=10)

    assert summary.status == "ok"
    assert summary.classified == 2
    recovery = [
        e for e in logs if e["event"] == "classification.backfill_recovery_failed"
    ]
    assert recovery and recovery[0]["error_type"] == "RuntimeError"


# --- admin endpoint: error visibility + one-run-at-a-time ----------------------


def test_background_task_wrapper_logs_plain_error_fields(monkeypatch) -> None:
    """The admin background wrapper must carry error_type/error as plain
    structured fields — exc_info alone is invisible in the Azure stream."""
    from tender_agent.api.admin import _run_classification_backfill_safely

    def _boom(**_kwargs):
        raise ValueError("backfill exploded")

    monkeypatch.setattr(backfill_module, "run_backfill", _boom)

    with capture_logs() as logs:
        _run_classification_backfill_safely(123)  # must not raise

    failed = [e for e in logs if e["event"] == "classification.backfill_failed"]
    assert failed
    assert failed[0]["error_type"] == "ValueError"
    assert "backfill exploded" in failed[0]["error"]
    assert failed[0]["limit"] == 123
    assert failed[0]["phase"] == "background_task"


def test_concurrent_backfill_trigger_returns_409(monkeypatch) -> None:
    """Only one backfill at a time: a second trigger while the first is
    still running gets 409; once the run finishes the lock is released and
    a new trigger is accepted."""
    from fastapi import BackgroundTasks, HTTPException

    from tender_agent.api import admin as admin_module
    from tender_agent.config import settings

    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    monkeypatch.setattr(backfill_module, "run_backfill", lambda **_kw: None)

    try:
        first = admin_module.trigger_classification_backfill(
            background_tasks=BackgroundTasks(), limit=5
        )
        assert first.status == "scheduled"

        with pytest.raises(HTTPException) as excinfo:
            admin_module.trigger_classification_backfill(
                background_tasks=BackgroundTasks(), limit=5
            )
        assert excinfo.value.status_code == 409

        # The background task finishing releases the lock...
        admin_module._run_classification_backfill_safely(5)
        # ...and a new trigger is accepted again.
        third = admin_module.trigger_classification_backfill(
            background_tasks=BackgroundTasks(), limit=5
        )
        assert third.status == "scheduled"
    finally:
        with contextlib.suppress(RuntimeError):
            admin_module._backfill_lock.release()
