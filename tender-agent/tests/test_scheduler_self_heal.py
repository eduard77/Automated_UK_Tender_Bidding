"""Scheduler self-heal + iteration logging + orphan cleanup.

Pins three Phase-1-continuation behaviours that the live readout
(2026-06-11 11:28Z) showed were missing:

  1. `ensure_sources()` runs at the START OF EVERY poll cycle, not only
     at boot. A new adapter wired in after the previous boot lands in
     the very next cycle — Atamis and EU-Supply showed `never_polled`
     while their Source rows did exist, so the more aggressive contract
     is the safer one.
  2. Every iteration emits a `scheduler.poll_source_attempt` structured
     log line. "Did we even try to poll X?" becomes answerable from the
     Azure Log stream without code archaeology.
  3. `_orphan_stale_running_runs()` marks PollRuns left in `running`
     past the deadline as `error`. The orphaned PROACTIS run the
     operator surfaced from 07:18 was exactly this shape.

Pure SQLAlchemy + structlog capture; no FastAPI app import, no Postgres.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from structlog.testing import capture_logs

from tender_agent import scheduler
from tender_agent.models import PollRun, Source
from tests._billing_fixtures import make_engine_and_session

NOW = datetime(2026, 6, 11, 12, 0, tzinfo=UTC)


@pytest.fixture()
def session_factory(monkeypatch):
    """Patch scheduler.SessionLocal to point at an in-memory SQLite."""
    _engine, factory = make_engine_and_session()
    monkeypatch.setattr(scheduler, "SessionLocal", factory)
    return factory


def _seed_source(factory, code: str, enabled: bool = True) -> int:
    with factory() as db:
        s = Source(code=code, name=code, base_url="x", enabled=enabled)
        db.add(s)
        db.commit()
        return s.id


# ---------------------------------------------------------------------------
# (1) self-heal: ensure_sources runs at top of poll_all
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poll_all_calls_ensure_sources_at_start(session_factory) -> None:
    """The contract this pins: a NEW adapter registered after boot lands
    in the next poll cycle without a redeploy. The fix calls
    ensure_sources() inside poll_all; the test asserts the call order."""
    seq: list[str] = []

    def fake_ensure() -> None:
        seq.append("ensure_sources")

    async def fake_poll_source(_db, source) -> None:
        seq.append(f"poll:{source.code}")

    _seed_source(session_factory, "CF")
    with (
        patch.object(scheduler, "ensure_sources", side_effect=fake_ensure),
        patch.object(scheduler, "poll_source", side_effect=fake_poll_source),
    ):
        await scheduler.poll_all()
    assert seq[0] == "ensure_sources"
    assert "poll:CF" in seq


# ---------------------------------------------------------------------------
# (2) per-iteration logging — "did we even try?"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poll_all_logs_attempt_per_source(session_factory) -> None:
    _seed_source(session_factory, "CF")
    _seed_source(session_factory, "ATAMIS")
    _seed_source(session_factory, "EU_SUPPLY")
    with (
        patch.object(scheduler, "ensure_sources"),
        patch.object(scheduler, "poll_source", new=AsyncMock(return_value=None)),
        capture_logs() as logs,
    ):
        await scheduler.poll_all()
    attempts = [
        ev for ev in logs if ev["event"] == "scheduler.poll_source_attempt"
    ]
    codes = [ev["source"] for ev in attempts]
    assert {"CF", "ATAMIS", "EU_SUPPLY"} <= set(codes), codes
    start_lines = [
        ev for ev in logs if ev["event"] == "scheduler.poll_all_starting"
    ]
    assert start_lines and start_lines[0]["source_count"] == 3


@pytest.mark.asyncio
async def test_poll_all_skips_sources_without_adapter_without_raising(
    session_factory,
) -> None:
    """A Source row whose code isn't in ADAPTERS (e.g. PROACTIS, which
    runs via its own browser-driven job) must NOT cause poll_source to
    raise inside the loop and abort the iteration of later sources."""
    _seed_source(session_factory, "CF")
    _seed_source(session_factory, "PROACTIS")  # no HTTP adapter
    _seed_source(session_factory, "ATAMIS")

    seen: list[str] = []

    async def fake_poll_source(_db, source) -> None:
        seen.append(source.code)

    with (
        patch.object(scheduler, "ensure_sources"),
        patch.object(scheduler, "poll_source", side_effect=fake_poll_source),
        capture_logs() as logs,
    ):
        await scheduler.poll_all()

    # CF and ATAMIS got polled; PROACTIS was skipped (no adapter).
    assert "CF" in seen and "ATAMIS" in seen
    assert "PROACTIS" not in seen
    proactis_attempt = next(
        ev
        for ev in logs
        if ev["event"] == "scheduler.poll_source_attempt"
        and ev["source"] == "PROACTIS"
    )
    assert proactis_attempt["has_adapter"] is False


# ---------------------------------------------------------------------------
# (3) orphan cleanup
# ---------------------------------------------------------------------------


def test_orphan_cleanup_marks_stale_running_runs_as_error(session_factory) -> None:
    """A PollRun left in `running` past the orphan deadline is flipped
    to status=`error` so the sources-health endpoint stops reporting a
    stuck cycle. Fresh `running` rows are left alone."""
    src_id = _seed_source(session_factory, "PROACTIS")
    cutoff = scheduler.ORPHAN_POLL_RUN_AFTER
    with session_factory() as db:
        stale = PollRun(
            source_id=src_id,
            started_at=NOW - cutoff - timedelta(minutes=10),
            status="running",
            fetched=0,
        )
        fresh = PollRun(
            source_id=src_id,
            started_at=NOW - timedelta(minutes=5),
            status="running",
            fetched=0,
        )
        db.add_all([stale, fresh])
        db.commit()

    with patch.object(scheduler, "datetime") as fake_dt:
        fake_dt.now.return_value = NOW
        with session_factory() as db:
            count = scheduler._orphan_stale_running_runs(db)
    assert count == 1

    with session_factory() as db:
        rows = db.execute(
            select(PollRun).order_by(PollRun.started_at)
        ).scalars().all()
        # SQLite drops tzinfo on round-trip; sort by id (insertion order)
        # is enough since we added stale before fresh.
        assert len(rows) == 2
        stale_row, fresh_row = rows[0], rows[1]
        assert stale_row.status == "error"
        assert (stale_row.error or "").startswith("orphaned")
        assert stale_row.finished_at is not None
        assert fresh_row.status == "running"


@pytest.mark.asyncio
async def test_poll_all_runs_orphan_cleanup(session_factory) -> None:
    _seed_source(session_factory, "CF")
    called: list[bool] = []

    def fake_orphan(_db) -> int:
        called.append(True)
        return 0

    with (
        patch.object(scheduler, "ensure_sources"),
        patch.object(scheduler, "_orphan_stale_running_runs", side_effect=fake_orphan),
        patch.object(scheduler, "poll_source", new=AsyncMock(return_value=None)),
    ):
        await scheduler.poll_all()
    assert called == [True]
