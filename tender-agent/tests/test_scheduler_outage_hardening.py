"""Scheduler outage hardening (2026-06-12 discovery outage).

The incident: a poll cycle hung forever (a stuck task meant `poll_all` never
returned; APScheduler's max_instances=1 then skipped every future cycle),
manual poll-now triggers ran CONCURRENTLY with scheduled cycles (trigger_now
bypassed the max_instances guard → the duplicate 11:23/11:25 batches), and
the health endpoint couldn't distinguish a dead scheduler from stale sources.

Pins the hardened contract:
  1. a hung source poll times out, its run is marked errored, the cycle
     completes, and the other sources still poll;
  2. the poll cycle is single-flight — a second invocation (scheduled or
     manual) is skipped while one runs;
  3. per-source single-flight — a source whose previous run is still
     genuinely active is skipped; a STALE `running` run is not respected;
  4. trigger_now reports the skip instead of double-firing;
  5. the scheduler heartbeat records cycle start/finish for the health
     endpoint.

Pure SQLAlchemy + structlog capture; no FastAPI app import, no Postgres.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import select
from structlog.testing import capture_logs

from tender_agent import scheduler
from tender_agent.models import PollRun, Source
from tests._billing_fixtures import make_engine_and_session


@pytest.fixture()
def session_factory(monkeypatch):
    _engine, factory = make_engine_and_session()
    monkeypatch.setattr(scheduler, "SessionLocal", factory)
    return factory


def _seed_source(factory, code: str) -> int:
    with factory() as db:
        source = Source(code=code, name=code, base_url="x", enabled=True)
        db.add(source)
        db.commit()
        return source.id


def _seed_running_run(factory, source_id: int, *, age: timedelta) -> int:
    with factory() as db:
        run = PollRun(
            source_id=source_id,
            started_at=datetime.now(UTC) - age,
            status="running",
            fetched=0,
        )
        db.add(run)
        db.commit()
        return run.id


# ---------------------------------------------------------------------------
# (1) hung source → timeout → run marked errored, cycle completes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hung_source_times_out_and_cycle_completes(
    session_factory, monkeypatch
) -> None:
    """THE stall regression: one source hanging forever used to wedge
    `poll_all` (and with max_instances=1, every future cycle). Now the hung
    source is cancelled on its timeout, its run is marked errored, and the
    cycle still finishes — with the other sources polled."""
    # Sub-second timeout so the test is fast: 0.005 minutes ≈ 0.3 s.
    monkeypatch.setattr(
        scheduler.settings, "poll_source_timeout_minutes", 0.005
    )
    cf_id = _seed_source(session_factory, "CF")
    _seed_source(session_factory, "FTS")

    polled: list[str] = []

    async def fake_poll_source(db, source) -> None:
        if source.code == "CF":
            # Simulate the hang — but first commit a `running` run row the
            # way the real poll_source does, so the timeout has something
            # to mark.
            db.add(PollRun(source_id=source.id, status="running", fetched=0))
            db.commit()
            await asyncio.sleep(3600)
        polled.append(source.code)

    with (
        patch.object(scheduler, "ensure_sources"),
        patch.object(scheduler, "poll_source", side_effect=fake_poll_source),
        capture_logs() as logs,
    ):
        await asyncio.wait_for(scheduler.poll_all(), timeout=10)

    assert "FTS" in polled  # the healthy source still polled
    assert "CF" not in polled  # the hung one was cancelled
    timed_out = [
        e for e in logs if e["event"] == "scheduler.poll_source_timed_out"
    ]
    assert timed_out and timed_out[0]["source"] == "CF"
    # The hung run was flipped to a self-explanatory error.
    with session_factory() as db:
        run = (
            db.execute(
                select(PollRun).where(PollRun.source_id == cf_id)
            )
            .scalars()
            .one()
        )
        assert run.status == "error"
        assert "timed out" in (run.error or "")
        assert run.finished_at is not None


# ---------------------------------------------------------------------------
# (2) global single-flight
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_second_cycle_is_skipped_while_one_runs(session_factory) -> None:
    _seed_source(session_factory, "CF")
    release = asyncio.Event()

    async def slow_poll_source(_db, _source) -> None:
        await release.wait()

    with (
        patch.object(scheduler, "ensure_sources"),
        patch.object(scheduler, "poll_source", side_effect=slow_poll_source),
        capture_logs() as logs,
    ):
        first = asyncio.create_task(scheduler.poll_all())
        await asyncio.sleep(0.05)  # let the first cycle take the lock
        await scheduler.poll_all()  # must skip, not queue or run
        release.set()
        await asyncio.wait_for(first, timeout=5)

    skips = [
        e
        for e in logs
        if e["event"] == "scheduler.poll_cycle_skipped_already_running"
    ]
    assert skips, "second cycle was not skipped"


@pytest.mark.asyncio
async def test_trigger_now_reports_skip_while_cycle_runs(
    session_factory,
) -> None:
    _seed_source(session_factory, "CF")
    release = asyncio.Event()

    async def slow_poll_source(_db, _source) -> None:
        await release.wait()

    with (
        patch.object(scheduler, "ensure_sources"),
        patch.object(scheduler, "poll_source", side_effect=slow_poll_source),
    ):
        first = asyncio.create_task(scheduler.poll_all())
        await asyncio.sleep(0.05)
        started = await scheduler.trigger_now()
        assert started is False  # single-flight: no concurrent manual cycle
        release.set()
        await asyncio.wait_for(first, timeout=5)
        # After the cycle finishes a manual trigger fires normally again.
        started_after = await scheduler.trigger_now()
        assert started_after is True
        await asyncio.sleep(0.05)
        release.set()
        # Drain the background task so it doesn't leak into other tests.
        for task in list(scheduler._background_tasks):
            await asyncio.wait_for(task, timeout=5)


# ---------------------------------------------------------------------------
# (3) per-source single-flight — skip genuinely-active, ignore stale
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_source_with_active_run_is_skipped(session_factory) -> None:
    """Cross-process duplicate-poll guard: a source whose newest run is
    `running` and recent (e.g. the other app instance during a deploy
    overlap is mid-poll) is skipped this cycle."""
    cf_id = _seed_source(session_factory, "CF")
    _seed_source(session_factory, "FTS")
    _seed_running_run(session_factory, cf_id, age=timedelta(minutes=1))

    polled: list[str] = []

    async def fake_poll_source(_db, source) -> None:
        polled.append(source.code)

    with (
        patch.object(scheduler, "ensure_sources"),
        patch.object(scheduler, "poll_source", side_effect=fake_poll_source),
        capture_logs() as logs,
    ):
        await asyncio.wait_for(scheduler.poll_all(), timeout=5)

    assert "FTS" in polled
    assert "CF" not in polled
    skipped = [
        e for e in logs if e["event"] == "scheduler.poll_source_skipped_active"
    ]
    assert skipped and skipped[0]["source"] == "CF"


@pytest.mark.asyncio
async def test_stale_running_run_does_not_block_polling(session_factory) -> None:
    """A `running` run older than the active-run grace is a leftover from a
    crash, not a live poll — the source must still be polled (the orphan
    cleanup also flips such rows at cycle start)."""
    cf_id = _seed_source(session_factory, "CF")
    _seed_running_run(session_factory, cf_id, age=timedelta(hours=3))

    polled: list[str] = []

    async def fake_poll_source(_db, source) -> None:
        polled.append(source.code)

    with (
        patch.object(scheduler, "ensure_sources"),
        patch.object(scheduler, "poll_source", side_effect=fake_poll_source),
    ):
        await asyncio.wait_for(scheduler.poll_all(), timeout=5)

    assert "CF" in polled


# ---------------------------------------------------------------------------
# (4) heartbeat
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_heartbeat_records_cycle_start_and_finish(session_factory) -> None:
    _seed_source(session_factory, "CF")

    async def fake_poll_source(_db, _source) -> None:
        beat = scheduler.heartbeat()
        assert beat["cycle_running"] is True  # visible mid-cycle

    before = datetime.now(UTC)
    with (
        patch.object(scheduler, "ensure_sources"),
        patch.object(scheduler, "poll_source", side_effect=fake_poll_source),
    ):
        await asyncio.wait_for(scheduler.poll_all(), timeout=5)

    beat = scheduler.heartbeat()
    assert beat["cycle_running"] is False
    assert beat["last_cycle_started_at"] is not None
    assert beat["last_cycle_started_at"] >= before
    assert beat["last_cycle_finished_at"] is not None
    assert beat["last_cycle_finished_at"] >= beat["last_cycle_started_at"]
    assert beat["last_cycle_error"] is None
    assert beat["process_started_at"] is not None


@pytest.mark.asyncio
async def test_cycle_body_exception_is_contained_and_recorded(
    session_factory,
) -> None:
    """The scheduler job must survive ANY cycle failure: the exception is
    swallowed (the next interval fires normally) and surfaced on the
    heartbeat for the health endpoint."""
    with patch.object(
        scheduler, "_run_poll_cycle", side_effect=RuntimeError("cycle boom")
    ):
        await scheduler.poll_all()  # must not raise

    beat = scheduler.heartbeat()
    assert beat["last_cycle_error"] is not None
    assert "cycle boom" in beat["last_cycle_error"]
    assert beat["cycle_running"] is False
