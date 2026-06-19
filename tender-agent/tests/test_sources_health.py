"""GET /admin/diagnostics/sources-health — the Phase-1 silent-source
diagnostic, offline.

In-memory SQLite stands in for Postgres; the auth dependency is overridden
the same way the other admin-diagnostic tests do. Pins the FIVE diagnosis
classes the endpoint maps the harness's failure modes onto:

  never_polled            — Source row exists, no PollRun ever (scheduler /
                            registration class)
  fetch_failing           — newest run errored (the error string then says
                            403 vs 500 vs DNS) AND made no forward progress
  catching_up             — newest run errored/timed-out but its resume
                            watermark ADVANCED vs the previous run — a long
                            backlog drain converging, not a fault (2026-06-14)
  polling_but_zero_rows   — clean runs, zero tenders (the watermark-trap /
                            format class)
  stale_not_polling       — newest run is clean but finished many poll
                            intervals ago (added 2026-06-11 rev 3 after a
                            "healthy"-but-9-days-old readout)
  needs_login             — browser-driven source whose newest run ended
                            waiting for a human login (PROACTIS)
  manual_browser_discovery — browser-driven source (PROACTIS) on its OWN
                            login-gated browser cycle, NOT the HTTP poll
                            loop; an older last-run is expected, not a fault
                            (added 2026-06-14)
  healthy                 — recent clean runs, rows present
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from tender_agent.api.admin_diagnostics import STALE_POLL_FACTOR
from tender_agent.api.deps import current_account
from tender_agent.config import settings
from tender_agent.db import get_db
from tender_agent.main import app
from tender_agent.models import PollRun, Source, Tender
from tests._billing_fixtures import make_engine_and_session

NOW = datetime(2026, 6, 11, 12, 0, tzinfo=UTC)

#: Freshness is judged by the endpoint against the real wall clock
#: (``datetime.now(UTC)``), not a fixture constant: a clean run that finished
#: more than ``poll_interval_minutes * STALE_POLL_FACTOR`` ago reads
#: ``stale_not_polling``, not ``healthy``. Fixtures that must read "healthy"
#: therefore anchor their newest run to the real clock and derive their
#: offsets from this window — one interval back is comfortably fresh, one
#: interval past the window is unambiguously stale — rather than a brittle
#: hardcoded gap that the fixed ``NOW`` above no longer satisfies.
POLL_INTERVAL = timedelta(minutes=settings.poll_interval_minutes)
STALE_WINDOW = POLL_INTERVAL * STALE_POLL_FACTOR


@pytest.fixture()
def factory():
    _engine, factory = make_engine_and_session()
    return factory


@pytest.fixture()
def client(factory):
    def override():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override
    app.dependency_overrides[current_account] = lambda: object()
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(current_account, None)


def _seed(factory) -> None:
    now = datetime.now(UTC)
    # One interval back is well inside the staleness window; one interval past
    # the window is unambiguously stale. Both derive from the real "now" the
    # endpoint diagnoses against, not the fixed NOW constant.
    fresh = now - POLL_INTERVAL
    stale = now - STALE_WINDOW - POLL_INTERVAL
    with factory() as db:
        healthy = Source(code="CF", name="Contracts Finder", base_url="x", enabled=True)
        failing = Source(code="S2W", name="Sell2Wales", base_url="x", enabled=True)
        zero = Source(code="EU_SUPPLY", name="EU-Supply", base_url="x", enabled=True)
        never = Source(code="ATAMIS", name="Atamis", base_url="x", enabled=True)
        idle = Source(
            code="PCS", name="Public Contracts Scotland", base_url="x", enabled=True
        )
        db.add_all([healthy, failing, zero, never, idle])
        db.commit()
        db.add_all(
            [
                # CF: recent clean run + a tender => healthy.
                PollRun(
                    source_id=healthy.id,
                    started_at=fresh - timedelta(minutes=1),
                    finished_at=fresh,
                    status="ok",
                    fetched=3,
                    new_count=2,
                ),
                # S2W: errored newest run => fetch_failing (the error branch
                # is decided on the newest run, ahead of any staleness check).
                PollRun(
                    source_id=failing.id,
                    started_at=fresh - timedelta(minutes=1),
                    finished_at=fresh,
                    status="error",
                    error="upstream HTTP requests failed (see adapter log events)",
                ),
                # EU_SUPPLY: recent clean run, zero rows — the watermark trap's
                # shape. Must be fresh, else staleness would mask the zero-rows
                # diagnosis (stale is checked before the zero-rows branch).
                PollRun(
                    source_id=zero.id,
                    started_at=fresh - timedelta(minutes=1),
                    finished_at=fresh,
                    status="ok",
                    fetched=0,
                ),
                # PCS: clean run + a tender, but it finished long ago — rows are
                # present yet the scheduler stopped firing => stale_not_polling.
                PollRun(
                    source_id=idle.id,
                    started_at=stale - timedelta(minutes=1),
                    finished_at=stale,
                    status="ok",
                    fetched=10,
                ),
                # ATAMIS: no PollRun at all.
            ]
        )
        db.add_all(
            [
                Tender(
                    source_code="CF",
                    source_ref="cf-1",
                    title="t",
                    first_seen_at=now,
                    last_seen_at=now,
                ),
                Tender(
                    source_code="PCS",
                    source_ref="pcs-1",
                    title="t",
                    first_seen_at=now,
                    last_seen_at=now,
                ),
            ]
        )
        db.commit()


def test_health_classifies_all_four_diagnosis_classes(client, factory) -> None:
    # Also pins the fifth class (stale_not_polling, PCS) added in rev 3.
    _seed(factory)
    resp = client.get("/admin/diagnostics/sources-health")
    assert resp.status_code == 200
    body = resp.json()
    by_code = {s["code"]: s for s in body["sources"]}

    assert by_code["CF"]["diagnosis"] == "healthy"
    assert by_code["CF"]["tender_count"] == 1
    assert by_code["CF"]["latest_runs"][0]["status"] == "ok"

    assert by_code["S2W"]["diagnosis"] == "fetch_failing"
    assert "upstream HTTP requests failed" in (
        by_code["S2W"]["latest_runs"][0]["error"] or ""
    )

    assert by_code["EU_SUPPLY"]["diagnosis"] == "polling_but_zero_rows"
    assert by_code["EU_SUPPLY"]["tender_count"] == 0

    # Newest run is clean but finished many intervals ago — not healthy.
    assert by_code["PCS"]["diagnosis"] == "stale_not_polling"
    assert by_code["PCS"]["tender_count"] == 1
    assert by_code["PCS"]["latest_runs"][0]["status"] == "ok"

    assert by_code["ATAMIS"]["diagnosis"] == "never_polled"
    assert by_code["ATAMIS"]["latest_runs"] == []


def test_health_runs_are_newest_first_and_capped(client, factory) -> None:
    with factory() as db:
        src = Source(code="FTS", name="FTS", base_url="x", enabled=True)
        db.add(src)
        db.commit()
        for i in range(5):
            db.add(
                PollRun(
                    source_id=src.id,
                    started_at=NOW - timedelta(hours=5 - i),
                    finished_at=NOW - timedelta(hours=5 - i, minutes=-1),
                    status="ok",
                    fetched=i,
                )
            )
        db.commit()
    body = client.get("/admin/diagnostics/sources-health").json()
    fts = next(s for s in body["sources"] if s["code"] == "FTS")
    assert len(fts["latest_runs"]) == 3  # POLL_RUNS_PER_SOURCE
    fetched = [r["fetched"] for r in fts["latest_runs"]]
    assert fetched == [4, 3, 2]  # newest first


def test_health_newest_ok_run_beats_older_errored_run(client, factory) -> None:
    """Phase-1 rev 3 regression: when the NEWEST run is "ok" but an
    older run still has an error string, the source must NOT be
    reported `fetch_failing`. The 2026-06-11 12:52Z PROACTIS readout
    tripped this exact case under the old `any(r.error for r in runs)`
    rule.

    The newest run is anchored to the real clock (within the staleness
    window) so the expected result is plainly `healthy` — the point is
    "newest-ok beats older-errored", not staleness."""
    now = datetime.now(UTC)
    with factory() as db:
        src = Source(code="PROACTIS", name="Proactis", base_url="x", enabled=True)
        db.add(src)
        db.commit()
        db.add_all(
            [
                # Older errored run.
                PollRun(
                    source_id=src.id,
                    started_at=now - POLL_INTERVAL * 2,
                    finished_at=now - POLL_INTERVAL * 2 + timedelta(minutes=2),
                    status="error",
                    error="BridgeError: ERR_ABORTED on advertId=...",
                ),
                # Newest clean run — finished one interval ago, well inside the
                # staleness window, so the source reads healthy not stale.
                PollRun(
                    source_id=src.id,
                    started_at=now - POLL_INTERVAL,
                    finished_at=now - POLL_INTERVAL + timedelta(minutes=1),
                    status="ok",
                    fetched=200,
                    new_count=61,
                ),
            ]
        )
        db.add(
            Tender(
                source_code="PROACTIS",
                source_ref="p-1",
                title="t",
                first_seen_at=now,
                last_seen_at=now,
            )
        )
        db.commit()
    body = client.get("/admin/diagnostics/sources-health").json()
    proactis = next(s for s in body["sources"] if s["code"] == "PROACTIS")
    assert proactis["diagnosis"] == "healthy"


def test_health_stale_when_newest_run_finished_long_ago(client, factory) -> None:
    """Phase-1 rev 3: a "healthy" source whose newest run finished N
    poll-intervals ago is actually stale — the scheduler isn't
    firing. The 2026-06-11 12:52Z readout had CF/FTS/PCS reading
    healthy with newest-run timestamps from 2026-06-02 (nine days
    earlier across a 30-min interval)."""
    now = datetime.now(UTC)
    with factory() as db:
        src = Source(code="CF", name="Contracts Finder", base_url="x", enabled=True)
        db.add(src)
        db.commit()
        # One interval past the staleness window, against the real clock.
        finished = now - STALE_WINDOW - POLL_INTERVAL
        db.add(
            PollRun(
                source_id=src.id,
                started_at=finished - timedelta(minutes=1),
                finished_at=finished,
                status="ok",
                fetched=4541,
            )
        )
        db.add(
            Tender(
                source_code="CF",
                source_ref="cf-1",
                title="t",
                first_seen_at=now,
                last_seen_at=now,
            )
        )
        db.commit()
    body = client.get("/admin/diagnostics/sources-health").json()
    cf = next(s for s in body["sources"] if s["code"] == "CF")
    assert cf["diagnosis"] == "stale_not_polling"


def test_health_catching_up_when_watermark_advances(client, factory) -> None:
    """2026-06-14 CF/FTS backlog: a run that times out at 900s but ADVANCED
    its resume watermark past the previous run is draining a backlog, not
    failing. It must read `catching_up`, not `fetch_failing`, so a long
    catch-up doesn't look like an outage. Based on watermark movement across
    runs, not run status alone."""
    now = datetime.now(UTC)
    with factory() as db:
        src = Source(code="CF", name="Contracts Finder", base_url="x", enabled=True)
        db.add(src)
        db.commit()
        db.add_all(
            [
                # Older timed-out run reached 2026-06-06.
                PollRun(
                    source_id=src.id,
                    started_at=now - POLL_INTERVAL * 2,
                    finished_at=now - POLL_INTERVAL * 2 + timedelta(minutes=15),
                    status="error",
                    error="poll timed out after 900s and was cancelled",
                    watermark_at=datetime(2026, 6, 6, tzinfo=UTC),
                ),
                # Newest timed-out run reached 2026-06-09 — forward progress.
                PollRun(
                    source_id=src.id,
                    started_at=now - POLL_INTERVAL,
                    finished_at=now - POLL_INTERVAL + timedelta(minutes=15),
                    status="error",
                    error="poll timed out after 900s and was cancelled",
                    watermark_at=datetime(2026, 6, 9, tzinfo=UTC),
                ),
            ]
        )
        db.add(
            Tender(
                source_code="CF",
                source_ref="cf-1",
                title="t",
                first_seen_at=now,
                last_seen_at=now,
            )
        )
        db.commit()
    body = client.get("/admin/diagnostics/sources-health").json()
    cf = next(s for s in body["sources"] if s["code"] == "CF")
    assert cf["diagnosis"] == "catching_up"
    # The resume point is surfaced so the operator can watch it climb.
    assert cf["latest_runs"][0]["watermark_at"] is not None


def test_health_fetch_failing_when_watermark_does_not_advance(client, factory) -> None:
    """The genuine-failure case the catching_up signal must NOT mask: a run
    that errors WITHOUT advancing its resume watermark (same value as the
    previous run, or none at all) is stuck — `fetch_failing`, not
    `catching_up`."""
    now = datetime.now(UTC)
    stuck_at = datetime(2026, 6, 2, tzinfo=UTC)
    with factory() as db:
        src = Source(code="CF", name="Contracts Finder", base_url="x", enabled=True)
        db.add(src)
        db.commit()
        db.add_all(
            [
                PollRun(
                    source_id=src.id,
                    started_at=now - POLL_INTERVAL * 2,
                    finished_at=now - POLL_INTERVAL * 2 + timedelta(minutes=15),
                    status="error",
                    error="poll timed out after 900s and was cancelled",
                    watermark_at=stuck_at,
                ),
                # Newest run made NO forward progress — same watermark.
                PollRun(
                    source_id=src.id,
                    started_at=now - POLL_INTERVAL,
                    finished_at=now - POLL_INTERVAL + timedelta(minutes=15),
                    status="error",
                    error="poll timed out after 900s and was cancelled",
                    watermark_at=stuck_at,
                ),
            ]
        )
        db.add(
            Tender(
                source_code="CF",
                source_ref="cf-1",
                title="t",
                first_seen_at=now,
                last_seen_at=now,
            )
        )
        db.commit()
    body = client.get("/admin/diagnostics/sources-health").json()
    cf = next(s for s in body["sources"] if s["code"] == "CF")
    assert cf["diagnosis"] == "fetch_failing"


def test_health_catching_up_when_cursor_advances_but_watermark_frozen(
    client, factory
) -> None:
    """The 2026-06-15 newest-first case: the date-watermark FREEZES (stays
    NULL — advancing it would skip older unfetched pages), so on watermark
    alone the drain reads `fetch_failing` forever. But the pagination cursor
    advances run-to-run as the backlog drains, and that MUST read
    `catching_up`. This is the signal cursor-based resume added."""
    now = datetime.now(UTC)
    with factory() as db:
        src = Source(code="FTS", name="Find a Tender", base_url="x", enabled=True)
        db.add(src)
        db.commit()
        db.add_all(
            [
                # Older timed-out run: watermark frozen (newest-first feed),
                # cursor reached page 3.
                PollRun(
                    source_id=src.id,
                    started_at=now - POLL_INTERVAL * 2,
                    finished_at=now - POLL_INTERVAL * 2 + timedelta(minutes=15),
                    status="error",
                    error="poll timed out after 900s and was cancelled",
                    watermark_at=None,
                    resume_cursor="https://fts.invalid/cursor-page-3",
                ),
                # Newest run: watermark STILL frozen, but the cursor advanced to
                # page 7 — forward progress through the backlog.
                PollRun(
                    source_id=src.id,
                    started_at=now - POLL_INTERVAL,
                    finished_at=now - POLL_INTERVAL + timedelta(minutes=15),
                    status="error",
                    error="poll timed out after 900s and was cancelled",
                    watermark_at=None,
                    resume_cursor="https://fts.invalid/cursor-page-7",
                ),
            ]
        )
        db.add(
            Tender(
                source_code="FTS",
                source_ref="fts-1",
                title="t",
                first_seen_at=now,
                last_seen_at=now,
            )
        )
        db.commit()
    body = client.get("/admin/diagnostics/sources-health").json()
    fts = next(s for s in body["sources"] if s["code"] == "FTS")
    assert fts["diagnosis"] == "catching_up"
    # The cursor presence is surfaced so the operator can see a drain in flight.
    assert fts["latest_runs"][0]["has_resume_cursor"] is True


def test_health_fetch_failing_when_cursor_stuck_and_watermark_frozen(
    client, factory
) -> None:
    """The genuine-stuck case on a newest-first feed: watermark frozen AND the
    cursor unchanged run-to-run (each cycle times out fetching the same page,
    fetching 0). Neither resume signal advanced — `fetch_failing`, not
    `catching_up`."""
    now = datetime.now(UTC)
    stuck_cursor = "https://fts.invalid/cursor-page-3"
    with factory() as db:
        src = Source(code="FTS", name="Find a Tender", base_url="x", enabled=True)
        db.add(src)
        db.commit()
        db.add_all(
            [
                PollRun(
                    source_id=src.id,
                    started_at=now - POLL_INTERVAL * 2,
                    finished_at=now - POLL_INTERVAL * 2 + timedelta(minutes=15),
                    status="error",
                    error="poll timed out after 900s and was cancelled",
                    watermark_at=None,
                    resume_cursor=stuck_cursor,
                ),
                PollRun(
                    source_id=src.id,
                    started_at=now - POLL_INTERVAL,
                    finished_at=now - POLL_INTERVAL + timedelta(minutes=15),
                    status="error",
                    error="poll timed out after 900s and was cancelled",
                    watermark_at=None,
                    resume_cursor=stuck_cursor,  # unchanged — no progress
                ),
            ]
        )
        db.add(
            Tender(
                source_code="FTS",
                source_ref="fts-1",
                title="t",
                first_seen_at=now,
                last_seen_at=now,
            )
        )
        db.commit()
    body = client.get("/admin/diagnostics/sources-health").json()
    fts = next(s for s in body["sources"] if s["code"] == "FTS")
    assert fts["diagnosis"] == "fetch_failing"


def test_health_browser_source_stale_run_reads_as_note_not_fault(client, factory) -> None:
    """2026-06-14: PROACTIS is browser-driven (login-gated, run on its OWN
    cycle via run_for_profile / proactis_discovery_job), NOT part of the HTTP
    poll loop — so it isn't in ADAPTERS. Judging its freshness against the
    HTTP poll interval is a category error: an older last-run is EXPECTED.
    A stale clean run must therefore read `manual_browser_discovery` (a note)
    rather than `stale_not_polling` (which implies a dead scheduler)."""
    now = datetime.now(UTC)
    finished = now - STALE_WINDOW - POLL_INTERVAL  # well past the HTTP window
    with factory() as db:
        src = Source(code="PROACTIS", name="Proactis", base_url="x", enabled=True)
        db.add(src)
        db.commit()
        db.add(
            PollRun(
                source_id=src.id,
                started_at=finished - timedelta(minutes=2),
                finished_at=finished,
                status="ok",
                fetched=200,
                new_count=61,
            )
        )
        db.add(
            Tender(
                source_code="PROACTIS",
                source_ref="p-1",
                title="t",
                first_seen_at=now,
                last_seen_at=now,
            )
        )
        db.commit()
    body = client.get("/admin/diagnostics/sources-health").json()
    proactis = next(s for s in body["sources"] if s["code"] == "PROACTIS")
    assert proactis["diagnosis"] == "manual_browser_discovery"


def test_health_browser_source_needs_login_run(client, factory) -> None:
    """A browser-driven discovery run finalises as `needs_login` when the
    bridge isn't authenticated. The endpoint surfaces that as the actionable
    state, not a generic class — the operator knows to log in at the bridge."""
    now = datetime.now(UTC)
    with factory() as db:
        src = Source(code="PROACTIS", name="Proactis", base_url="x", enabled=True)
        db.add(src)
        db.commit()
        db.add(
            PollRun(
                source_id=src.id,
                started_at=now - POLL_INTERVAL,
                finished_at=now - POLL_INTERVAL + timedelta(minutes=1),
                status="needs_login",
                fetched=0,
            )
        )
        db.commit()
    body = client.get("/admin/diagnostics/sources-health").json()
    proactis = next(s for s in body["sources"] if s["code"] == "PROACTIS")
    assert proactis["diagnosis"] == "needs_login"


def test_health_includes_scheduler_heartbeat(client, factory) -> None:
    """2026-06-12 outage: per-source diagnoses said `stale_not_polling` while
    the actual fault was a DEAD scheduler. The payload now carries a
    top-level scheduler heartbeat so the two are distinguishable at a
    glance. (No scheduler runs under tests → running=False.)"""
    resp = client.get("/admin/diagnostics/sources-health")
    assert resp.status_code == 200
    body = resp.json()

    assert "scheduler" in body
    beat = body["scheduler"]
    assert set(beat) == {
        "scheduler_running",
        "process_started_at",
        "last_cycle_started_at",
        "last_cycle_finished_at",
        "cycle_running",
        "last_cycle_error",
        "next_cycle_at",
    }
    assert beat["scheduler_running"] is False  # not started in tests
    assert beat["process_started_at"] is not None


def test_health_rejects_anonymous(factory) -> None:
    def override():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override
    try:
        anon = TestClient(app)
        resp = anon.get("/admin/diagnostics/sources-health")
        assert resp.status_code == 401
    finally:
        app.dependency_overrides.pop(get_db, None)
