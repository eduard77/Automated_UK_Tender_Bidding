"""Backlog catch-up is RESUMABLE (2026-06-14 CF/FTS 900s loop).

CF and FTS carry ~12 days of backlog that a full catch-up sweep can't drain
inside the 900s per-source poll timeout. The timeout cancels the run (correct
self-heal), but progress used to be DISCARDED on cancellation — so the next
run restarted from the backlog start and timed out again, forever, never
advancing the watermark.

Root cause: `asyncio.wait_for` cancels poll_source with a ``CancelledError``,
a BaseException that bypasses poll_source's ``except Exception`` AND its final
watermark-persistence/commit. poll_source now persists the resume watermark
INCREMENTALLY as each page confirms (committed there and then), so a cancelled
mid-sweep run keeps everything fetched so far and the next run resumes forward.

These drive the SHARED poll_source machinery with a fake paging adapter that
sets the same ``progress_watermark`` CF and FTS set per confirmed page, and
cancel it with the EXACT ``asyncio.wait_for`` the scheduler uses. The fake
tenders carry no date columns: ``_upsert_tender`` serialises with
``model_dump(mode="json")`` (ISO-string dates), which Postgres accepts but
SQLite's DateTime column rejects — and the resume watermark comes from the
adapter's ``progress_watermark``, not the tender dates, so omitting them
changes nothing under test. The real CF/FTS per-page watermark + the FTS
salvage/continue are covered in test_adapter_cf / test_adapter_fts.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import func, select

from tender_agent.adapters.base import SourceAdapter
from tender_agent.models import Source, Tender
from tender_agent.schemas import NormalisedTender
from tender_agent.services.ingestion import poll_source
from tests._billing_fixtures import make_engine_and_session

# Watermark values the fake adapters report per confirmed page (NOT tender
# published_at — see the module docstring on the SQLite DateTime quirk).
T1 = datetime(2026, 6, 5, 10, 0, tzinfo=UTC)
T2 = datetime(2026, 6, 6, 10, 0, tzinfo=UTC)
T3 = datetime(2026, 6, 7, 10, 0, tzinfo=UTC)
T4 = datetime(2026, 6, 8, 10, 0, tzinfo=UTC)

ADAPTERS_PATH = "tender_agent.services.ingestion.ADAPTERS"


def _t(code: str, ref: str) -> NormalisedTender:
    return NormalisedTender(source_code=code, source_ref=ref, title=f"t-{ref}")


def _safe_upsert(db, normalised):
    """SQLite-safe stand-in for _upsert_tender: writes a REAL row (so DB
    durability across sessions is genuinely exercised) without the ARRAY /
    ISO-string-date columns that production's model_dump(mode="json") path
    binds and that SQLite's driver rejects. The resume logic under test reads
    the adapter's progress_watermark, never these rows. Idempotent on
    (source_code, source_ref) like the real upsert, so a resume that
    re-fetches an overlapping page doesn't trip the unique constraint."""
    now = datetime.now(UTC)
    existing = db.execute(
        select(Tender).where(
            Tender.source_code == normalised.source_code,
            Tender.source_ref == normalised.source_ref,
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.last_seen_at = now
        return existing, "unchanged"
    tender = Tender(
        source_code=normalised.source_code,
        source_ref=normalised.source_ref,
        title=normalised.title,
        first_seen_at=now,
        last_seen_at=now,
        content_hash=normalised.source_ref,
    )
    db.add(tender)
    db.flush()
    return tender, "new"


# Patch targets that keep poll_source's per-record body SQLite-safe and
# deterministic while leaving its loop + incremental-watermark control flow —
# the thing under test — entirely real.
_PATCH_UPSERT = patch(
    "tender_agent.services.ingestion._upsert_tender", _safe_upsert
)
_PATCH_PORTALS = patch(
    "tender_agent.services.ingestion.process_tender_for_portals",
    lambda tender, db: SimpleNamespace(portal_ids_queued=[]),
)


def _aware(value: datetime | None) -> datetime | None:
    """SQLite drops tzinfo on round-trip; treat a naive value as UTC."""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


@pytest.mark.parametrize("code", ["CF", "FTS"])
@pytest.mark.asyncio
async def test_cancelled_mid_sweep_persists_progress_and_resumes(code) -> None:
    """A 900s-timeout cancellation mid-sweep keeps progress, and the next run
    resumes FORWARD from it — no restart-from-backlog-start loop. Proven for
    both CF and FTS (the shared resume machinery; each sets progress_watermark
    per confirmed page)."""
    _engine, factory = make_engine_and_session()
    sinces: list[datetime] = []

    class _StallMidSweep(SourceAdapter):
        code = "FAKE"
        name = "fake"
        base_url = "https://example.invalid"

        async def fetch_since(self, since) -> AsyncIterator[NormalisedTender]:
            sinces.append(since)
            yield _t(code, "r1")
            self.progress_watermark = T1  # page 1 confirmed by page 2
            yield _t(code, "r2")  # poll_source persists T1 here
            self.progress_watermark = T2
            yield _t(code, "r3")  # poll_source persists T2 here
            self.progress_watermark = T3
            await asyncio.sleep(60)  # the 900s timeout lands HERE
            yield _t(code, "r4")  # never reached

    class _CompleteFromResume(SourceAdapter):
        code = "FAKE"
        name = "fake"
        base_url = "https://example.invalid"

        async def fetch_since(self, since) -> AsyncIterator[NormalisedTender]:
            sinces.append(since)
            yield _t(code, "r3")
            self.progress_watermark = T3
            yield _t(code, "r4")
            self.progress_watermark = T4
            # clean end → status ok, watermark reaches present

    with factory() as db:
        src = Source(code=code, name=code, base_url="x", enabled=True)
        db.add(src)
        db.commit()
        src_id = src.id

    # Run 1: cancelled mid-sweep by the scheduler's asyncio.wait_for self-heal.
    with (
        patch.dict(ADAPTERS_PATH, {code: _StallMidSweep}, clear=False),
        _PATCH_UPSERT,
        _PATCH_PORTALS,
        factory() as db,
    ):
        source = db.get(Source, src_id)
        with pytest.raises((TimeoutError, asyncio.TimeoutError)):
            await asyncio.wait_for(poll_source(db, source), timeout=1.0)

    # Progress SURVIVED the cancellation: last_polled_at advanced to the last
    # page confirmed before the stall (T2), not discarded back to the start.
    with factory() as db:
        source = db.get(Source, src_id)
        assert _aware(source.last_polled_at) == T2
        # and the records fetched so far are committed (not rolled back).
        n = db.execute(
            select(func.count(Tender.id)).where(Tender.source_code == code)
        ).scalar_one()
        assert n == 3  # r1, r2, r3

    # Run 2 resumes FORWARD from T2 (not the backlog start) and completes ok.
    with (
        patch.dict(ADAPTERS_PATH, {code: _CompleteFromResume}, clear=False),
        _PATCH_UPSERT,
        _PATCH_PORTALS,
        factory() as db,
    ):
        source = db.get(Source, src_id)
        run = await poll_source(db, source)
        assert run.status == "ok"

    assert _aware(sinces[-1]) == T2  # the second run started from the resume point


@pytest.mark.asyncio
async def test_fts_salvage_commits_records_and_advances_then_a_timeout_resumes() -> None:
    """The FTS page-14 shape through the SHARED poll_source path: a salvaged
    page's records are COMMITTED and the resume watermark ADVANCES past page 1
    BEFORE the sweep continues — so when a timeout then cancels mid-continuation
    the salvaged records are kept and the next run resumes after them, never
    restarting before the bad page. (The real salvage/continue mechanics are in
    test_adapter_fts; here we prove poll_source commits + persists them.)"""
    _engine, factory = make_engine_and_session()

    class _SalvageThenStall(SourceAdapter):
        code = "FTS"
        name = "fts"
        base_url = "https://example.invalid"

        async def fetch_since(self, since) -> AsyncIterator[NormalisedTender]:
            yield _t("FTS", "p1")
            self.progress_watermark = T1  # page 1 confirmed
            # page 2 is SALVAGED: one bad notice failed alone, the rest yield.
            self.had_errors = True
            self.record_error(
                "page 2: malformed JSON at char 665438 of 1053875 — salvaged "
                "2 releases (1 skipped regions); recovered next cursor, CONTINUING"
            )
            yield _t("FTS", "p2a")  # salvaged record — committed per-record
            yield _t("FTS", "p2b")  # salvaged record — committed per-record
            self.progress_watermark = T2  # salvaged page confirmed → advance
            yield _t("FTS", "p3a")  # CONTINUED past the bad page (poll_source persists T2)
            self.progress_watermark = T3
            await asyncio.sleep(60)  # a later timeout lands mid-continuation
            yield _t("FTS", "p3b")  # never reached

    with factory() as db:
        src = Source(code="FTS", name="FTS", base_url="x", enabled=True)
        db.add(src)
        db.commit()
        src_id = src.id

    with (
        patch.dict(ADAPTERS_PATH, {"FTS": _SalvageThenStall}, clear=False),
        _PATCH_UPSERT,
        _PATCH_PORTALS,
        factory() as db,
    ):
        source = db.get(Source, src_id)
        with pytest.raises((TimeoutError, asyncio.TimeoutError)):
            await asyncio.wait_for(poll_source(db, source), timeout=1.0)

    with factory() as db:
        source = db.get(Source, src_id)
        refs = set(
            db.execute(
                select(Tender.source_ref).where(Tender.source_code == "FTS")
            ).scalars()
        )
        # Salvaged records committed BEFORE the continuation, and the
        # continuation page's record too — none lost to the cancellation.
        assert {"p1", "p2a", "p2b", "p3a"} <= refs
        # Resume point advanced PAST the salvaged page (to T2), so the next run
        # resumes after the bad page, never restarting before it.
        assert _aware(source.last_polled_at) == T2
