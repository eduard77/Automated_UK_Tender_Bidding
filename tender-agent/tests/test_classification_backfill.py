"""Classification backfill tests (Phase 3a) — the Batch-API path.

Offline: a fake batch client stands in for `client.messages.batches`
(create / retrieve / results). Proves the backfill is bounded, resumable,
idempotent, and that a still-processing batch reports `pending` without
writing anything.
"""
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from tender_agent.models import Tender
from tender_agent.services.classification.backfill import run_backfill
from tender_agent.services.classification.taxonomy import CLASSIFIER_VERSION
from tests._billing_fixtures import make_engine_and_session

NOW = datetime(2026, 6, 12, 12, 0, tzinfo=UTC)

_CONSTRUCTION = (
    '{"primary_sector": "Construction & Built Environment",'
    ' "secondary_sectors": [], '
    '"construction_subcategories": ["Refurbishment & renovation"]}'
)


def _noop_sleep(_seconds) -> None:
    return None


# --- fake Batch-API client -------------------------------------------------


class _FakeBatches:
    def __init__(self, *, response_fn, statuses) -> None:
        self._response_fn = response_fn
        self._statuses = list(statuses)
        self.created_requests: list[dict] | None = None

    def create(self, *, requests):
        self.created_requests = list(requests)
        return SimpleNamespace(id="batch_test", processing_status=self._statuses[0])

    def retrieve(self, batch_id):
        if len(self._statuses) > 1:
            self._statuses.pop(0)
        return SimpleNamespace(id=batch_id, processing_status=self._statuses[0])

    def results(self, _batch_id):
        for req in self.created_requests or []:
            custom_id = req["custom_id"]
            text = self._response_fn(custom_id)
            if text is None:
                yield SimpleNamespace(
                    custom_id=custom_id,
                    result=SimpleNamespace(type="errored", message=None),
                )
                continue
            message = SimpleNamespace(
                content=[SimpleNamespace(type="text", text=text)],
                usage=SimpleNamespace(input_tokens=8, output_tokens=4),
            )
            yield SimpleNamespace(
                custom_id=custom_id,
                result=SimpleNamespace(type="succeeded", message=message),
            )


class _FakeBatchClient:
    def __init__(self, *, response_fn=None, statuses=("ended",)) -> None:
        fn = response_fn or (lambda _cid: _CONSTRUCTION)
        self.messages = SimpleNamespace(
            batches=_FakeBatches(response_fn=fn, statuses=statuses)
        )


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


# --- tests -----------------------------------------------------------------


def test_backfill_classifies_pool_and_logs_tokens() -> None:
    _engine, factory = make_engine_and_session()
    _seed_tenders(factory, 3)

    summary = run_backfill(
        factory,
        limit=10,
        client=_FakeBatchClient(),
        poll_interval_s=0,
        sleep=_noop_sleep,
    )

    assert summary.status == "ok"
    assert summary.submitted == 3
    assert summary.classified == 3
    assert summary.input_tokens == 24 and summary.output_tokens == 12
    with factory() as db:
        sectors = {
            t.primary_sector for t in db.query(Tender).all()
        }
        assert sectors == {"Construction & Built Environment"}
        assert all(
            t.classifier_version == CLASSIFIER_VERSION
            for t in db.query(Tender).all()
        )


def test_backfill_is_bounded_by_limit() -> None:
    _engine, factory = make_engine_and_session()
    _seed_tenders(factory, 5)

    summary = run_backfill(
        factory, limit=2, client=_FakeBatchClient(), poll_interval_s=0,
        sleep=_noop_sleep,
    )

    assert summary.submitted == 2
    assert summary.classified == 2
    with factory() as db:
        classified = db.query(Tender).filter(Tender.primary_sector.isnot(None)).count()
        unclassified = db.query(Tender).filter(Tender.primary_sector.is_(None)).count()
        assert classified == 2 and unclassified == 3


def test_backfill_is_idempotent_and_resumable() -> None:
    _engine, factory = make_engine_and_session()
    _seed_tenders(factory, 3)

    run_backfill(factory, limit=10, client=_FakeBatchClient(), poll_interval_s=0,
                 sleep=_noop_sleep)
    # Re-run: every tender is already on the current version → nothing to do.
    again = run_backfill(
        factory, limit=10, client=_FakeBatchClient(), poll_interval_s=0,
        sleep=_noop_sleep,
    )
    assert again.status == "empty"
    assert again.submitted == 0


def test_backfill_pending_batch_writes_nothing() -> None:
    _engine, factory = make_engine_and_session()
    _seed_tenders(factory, 2)

    summary = run_backfill(
        factory,
        limit=10,
        client=_FakeBatchClient(statuses=("in_progress",)),
        poll_interval_s=0.001,
        max_wait_s=0.005,
        sleep=_noop_sleep,
    )

    assert summary.status == "pending"
    assert summary.submitted == 2
    assert summary.classified == 0
    with factory() as db:
        assert db.query(Tender).filter(Tender.primary_sector.isnot(None)).count() == 0


def test_backfill_errored_result_skips_that_tender_only() -> None:
    _engine, factory = make_engine_and_session()
    ids = _seed_tenders(factory, 3)
    bad = f"tender-{ids[1]}"

    summary = run_backfill(
        factory,
        limit=10,
        client=_FakeBatchClient(
            response_fn=lambda cid: None if cid == bad else _CONSTRUCTION
        ),
        poll_interval_s=0,
        sleep=_noop_sleep,
    )

    assert summary.submitted == 3
    assert summary.classified == 2
    assert summary.errored == 1
    with factory() as db:
        assert db.get(Tender, ids[1]).primary_sector is None  # the errored one
        assert db.get(Tender, ids[0]).primary_sector == (
            "Construction & Built Environment"
        )
