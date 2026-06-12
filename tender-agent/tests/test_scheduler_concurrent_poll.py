"""Scheduler concurrent per-source polling (Phase-1 rev 4, 2026-06-12).

The live Log stream proved the poll cycle was SERIAL: it spent over a
minute still inside FTS (streaming 160+ tenders) and never reached the
tail sources (EU_SUPPLY/ATAMIS), which is why they read `never_polled`.
Decoupling enrichment (rev 3) took the AI call off the hot path but the
ingestion VOLUME itself still monopolised the cycle.

`poll_all` now fans each source out onto its own task + DB session via
`asyncio.gather`, bounded by a semaphore, so a high-volume source can no
longer starve the others. These tests pin that contract:

  1. a high-volume source does not starve the sources after it,
  2. one source raising doesn't stop its siblings,
  3. each source polls on its OWN session (never a shared one).

Pure SQLAlchemy + structlog capture; no FastAPI app import, no Postgres.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
from structlog.testing import capture_logs

from tender_agent import scheduler
from tender_agent.models import Source
from tests._billing_fixtures import make_engine_and_session


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


@pytest.mark.asyncio
async def test_high_volume_source_does_not_starve_later_sources(
    session_factory, monkeypatch
) -> None:
    """The core rev-4 regression. FTS is seeded first (smallest id, so it
    leads the cycle) and its fake poll only completes AFTER the tail
    sources have been polled. Under the OLD serial loop FTS would block
    them forever — `poll_all` would never return and the outer `wait_for`
    would time out. Under the concurrent fan-out the tail polls run,
    release FTS, and the cycle finishes."""
    # Make the cap comfortably exceed the source count so the test is
    # independent of the configured default.
    monkeypatch.setattr(scheduler.settings, "poll_max_concurrency", 8)
    _seed_source(session_factory, "FTS")
    _seed_source(session_factory, "EU_SUPPLY")
    _seed_source(session_factory, "ATAMIS")

    polled: list[str] = []
    tail_reached = asyncio.Event()

    async def fake_poll_source(_db, source) -> None:
        if source.code == "FTS":
            # High-volume source: only finishes once the tail is reached.
            await tail_reached.wait()
            polled.append("FTS")
            return
        polled.append(source.code)
        if {"EU_SUPPLY", "ATAMIS"} <= set(polled):
            tail_reached.set()

    with (
        patch.object(scheduler, "ensure_sources"),
        patch.object(scheduler, "poll_source", side_effect=fake_poll_source),
    ):
        await asyncio.wait_for(scheduler.poll_all(), timeout=5)

    # All three reached — and FTS finishing proves the tail ran while FTS
    # was still in-flight (it was waiting on them).
    assert {"FTS", "EU_SUPPLY", "ATAMIS"} <= set(polled)


@pytest.mark.asyncio
async def test_one_source_raising_does_not_stop_others(session_factory) -> None:
    """A source raising inside its task is logged and swallowed; the other
    sources in the same cycle still get polled (gather isolation)."""
    _seed_source(session_factory, "FTS")
    _seed_source(session_factory, "CF")
    _seed_source(session_factory, "EU_SUPPLY")

    polled: list[str] = []

    async def fake_poll_source(_db, source) -> None:
        if source.code == "CF":
            raise RuntimeError("boom")
        polled.append(source.code)

    with (
        patch.object(scheduler, "ensure_sources"),
        patch.object(scheduler, "poll_source", side_effect=fake_poll_source),
        capture_logs() as logs,
    ):
        await asyncio.wait_for(scheduler.poll_all(), timeout=5)

    assert {"FTS", "EU_SUPPLY"} <= set(polled)
    assert "CF" not in polled
    # The failure is logged (not raised) so it can't cancel siblings.
    failed = [ev for ev in logs if ev["event"] == "scheduler.source_failed"]
    assert any(ev.get("source") == "CF" for ev in failed)


@pytest.mark.asyncio
async def test_each_source_polls_on_its_own_session(session_factory) -> None:
    """Concurrency safety: every concurrent source gets a DISTINCT DB
    session — the cycle never threads one shared Session across tasks
    (SQLAlchemy Sessions are not concurrency-safe)."""
    _seed_source(session_factory, "FTS")
    _seed_source(session_factory, "CF")
    _seed_source(session_factory, "EU_SUPPLY")

    sessions: list[object] = []

    async def fake_poll_source(db, _source) -> None:
        sessions.append(db)

    with (
        patch.object(scheduler, "ensure_sources"),
        patch.object(scheduler, "poll_source", side_effect=fake_poll_source),
    ):
        await asyncio.wait_for(scheduler.poll_all(), timeout=5)

    assert len(sessions) == 3
    assert len({id(s) for s in sessions}) == 3  # all distinct
