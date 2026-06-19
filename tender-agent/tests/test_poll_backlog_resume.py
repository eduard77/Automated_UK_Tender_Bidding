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
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from sqlalchemy import func, select

from tender_agent.adapters.base import SourceAdapter
from tender_agent.adapters.contracts_finder import ContractsFinderAdapter
from tender_agent.adapters.fts import FTSAdapter
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
            # The adapter confirms its last page (T3) and sets the watermark
            # just before the next page's network fetch — modelled by this
            # sleep, where the 900s timeout cancellation lands. The persist
            # gap that loses THIS value is the bug poll_source's finally closes.
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

    # Progress SURVIVED the cancellation: last_polled_at advanced to T3 — the
    # watermark the adapter set immediately before the cancel-await, captured
    # by poll_source.s cancellation-persist (the page-boundary gap that used to
    # discard it). Not discarded back to the backlog start.
    with factory() as db:
        source = db.get(Source, src_id)
        assert _aware(source.last_polled_at) == T3
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

    assert _aware(sinces[-1]) == T3  # the second run started from the resume point


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
            # Next page (T3) confirmed, watermark set just before its fetch —
            # the cancel lands in that fetch and the cancellation-persist keeps T3.
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
        # Resume point advanced PAST the salvaged page (to T3 — the watermark
        # set just before the cancel-await, kept by the cancellation-persist), so
        # the next run resumes after the bad page, never restarting before it.
        assert _aware(source.last_polled_at) == T3


# ---------------------------------------------------------------------------
# End-to-end through the REAL CF/FTS adapters + tracker (not a fake watermark).
#
# The tests above drive the shared poll_source loop with a fake adapter that
# sets progress_watermark directly. These prove the OTHER half: that the real
# ContractsFinder/FTS adapters, paging a real (mock-transported) OCDS feed,
# actually PRODUCE an advancing watermark, and that a cancellation landing in
# the next page's network fetch keeps the page just committed. This is the gap
# the 2026-06-15 diagnosis found — every prior test faked the watermark, so the
# tracker never producing one in production was invisible.
# ---------------------------------------------------------------------------


def _ocds_release(ocid: str, date: str) -> dict:
    return {
        "ocid": ocid,
        "id": f"{ocid}-1",
        "date": date,
        "tag": ["tender"],
        "tender": {"title": f"Tender {ocid}", "status": "active"},
    }


def _ocds_page_bytes(dated: list[tuple[str, str]], next_url: str) -> bytes:
    page = {
        "releases": [_ocds_release(ocid, date) for ocid, date in dated],
        "links": {"next": next_url},
    }
    return json.dumps(page).encode()


_PAGE2_MARKER = "RESUME-PAGE-2"


class _BlockOnNextPage(httpx.AsyncBaseTransport):
    """Serves page 1 for any request, but BLOCKS (awaits forever, after
    signalling `reached`) on the page-2 fetch — the await where the real 900s
    cancellation lands once page 1 has been fully consumed."""

    def __init__(self, page1: bytes, reached: asyncio.Event) -> None:
        self._page1 = page1
        self._reached = reached

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if _PAGE2_MARKER in str(request.url):
            self._reached.set()
            await asyncio.sleep(3600)  # cancellation is injected here
        return httpx.Response(
            200, content=self._page1, headers={"Content-Type": "application/json"}
        )


async def _instant_sleep(_seconds: float) -> None:
    return None


def _real_adapter_factory(code: str, page1: bytes, reached: asyncio.Event):
    """A subclass of the REAL adapter that injects the blocking transport (and,
    for CF, neutralises the inter-page pacing sleep so the cancel lands in the
    page-2 fetch, not the delay)."""
    base = ContractsFinderAdapter if code == "CF" else FTSAdapter

    class _RealWithBlockingTransport(base):  # type: ignore[valid-type,misc]
        def __init__(self) -> None:
            super().__init__(
                client=httpx.AsyncClient(
                    transport=_BlockOnNextPage(page1, reached)
                )
            )
            self._sleep = _instant_sleep  # type: ignore[assignment]

    return _RealWithBlockingTransport


@pytest.mark.parametrize("code", ["CF", "FTS"])
@pytest.mark.asyncio
async def test_real_adapter_cancel_in_next_fetch_keeps_committed_page(code) -> None:
    """The live failure shape, end to end: page 1 (ascending, multi-record)
    commits, the watermark advances to page 1's max via the tracker, and a
    cancellation landing in the page-2 network fetch KEEPS that watermark —
    not null, not the backlog start. Proven through the real CF and FTS
    adapters (each must produce the watermark itself; #133 may have wired only
    one)."""
    page1_max = datetime(2026, 6, 12, 10, 0, tzinfo=UTC)
    page1 = _ocds_page_bytes(
        [
            ("r-a", "2026-06-10T10:00:00Z"),
            ("r-b", "2026-06-11T10:00:00Z"),
            ("r-c", "2026-06-12T10:00:00Z"),
        ],
        next_url=f"https://resume.invalid/{_PAGE2_MARKER}",
    )
    reached = asyncio.Event()
    adapter_cls = _real_adapter_factory(code, page1, reached)

    _engine, factory = make_engine_and_session()
    with factory() as db:
        src = Source(code=code, name=code, base_url="x", enabled=True)
        db.add(src)
        db.commit()
        src_id = src.id

    with (
        patch.dict(ADAPTERS_PATH, {code: adapter_cls}, clear=False),
        _PATCH_UPSERT,
        _PATCH_PORTALS,
        factory() as db,
    ):
        source = db.get(Source, src_id)
        task = asyncio.create_task(poll_source(db, source))
        # Page 1 is fully consumed by the time the transport reaches page 2.
        await asyncio.wait_for(reached.wait(), timeout=5.0)
        task.cancel()  # the 900s self-heal, landing in the page-2 fetch
        with pytest.raises(asyncio.CancelledError):
            await task

    with factory() as db:
        source = db.get(Source, src_id)
        # Page 1's three records were committed (not rolled back)...
        n = db.execute(
            select(func.count(Tender.id)).where(Tender.source_code == code)
        ).scalar_one()
        assert n == 3
        # ...and the resume watermark reflects page 1 — NOT null, NOT the
        # backlog start. The next run resumes strictly forward from here.
        assert _aware(source.last_polled_at) == page1_max


@pytest.mark.asyncio
async def test_persistence_path_runs_under_cancellederror_specifically() -> None:
    """Isolates the BaseException path that the original #131 bug skipped: a
    ``CancelledError`` (NOT an ``Exception``) injected mid-sweep must still
    reach the watermark-persist. The fake sets progress_watermark then blocks;
    we cancel the task (not via wait_for) so the ONLY way the watermark
    survives is the cancellation-persist running during CancelledError unwinding."""
    persisted: list[datetime] = []
    reached = asyncio.Event()

    class _SetThenBlock(SourceAdapter):
        code = "FAKE"
        name = "fake"
        base_url = "https://example.invalid"

        async def fetch_since(self, since) -> AsyncIterator[NormalisedTender]:
            yield _t("CF", "r1")
            self.progress_watermark = T1  # confirmed page; set before the await
            reached.set()
            await asyncio.sleep(3600)  # cancel lands here, after T1 is set
            yield _t("CF", "r2")  # never reached

    _engine, factory = make_engine_and_session()
    with factory() as db:
        src = Source(code="CF", name="CF", base_url="x", enabled=True)
        db.add(src)
        db.commit()
        src_id = src.id

    with (
        patch.dict(ADAPTERS_PATH, {"CF": _SetThenBlock}, clear=False),
        _PATCH_UPSERT,
        _PATCH_PORTALS,
        factory() as db,
    ):
        source = db.get(Source, src_id)
        task = asyncio.create_task(poll_source(db, source))
        await asyncio.wait_for(reached.wait(), timeout=5.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    with factory() as db:
        source = db.get(Source, src_id)
        persisted.append(_aware(source.last_polled_at))
    # The watermark set immediately before the cancel-await survived — the
    # cancellation-persist ran during the CancelledError, exactly the path the
    # ``except Exception`` block cannot catch.
    assert persisted == [T1]


# ---------------------------------------------------------------------------
# CURSOR-based resume (2026-06-15, 4th pass on the CF/FTS non-advancing loop).
#
# #134 made the date-watermark direction-aware: correct, but on a NEWEST-first
# feed it FREEZES (advancing would skip older unfetched pages), so a cancelled
# mid-sweep run kept no resume point and the next run re-walked from page 1 —
# the persistent `watermark_at: null while records stream` signature the
# 2026-06-15 diagnosis saw. The fix #134 named: resume by the OCDS `links.next`
# cursor, which is available per page REGARDLESS of feed direction. These tests
# drive the REAL CF/FTS adapters over a mock-transported cursor chain and prove
# the cursor — not the frozen date-watermark — is what drains the backlog.
# ---------------------------------------------------------------------------

# A 4-page NEWEST-first feed: page 1 carries the newest records, page 4 the
# oldest. The PageProgressTracker FREEZES on this ordering, so last_polled_at
# never advances — the cursor is the only thing that moves the drain forward.
_CHAIN = ["CHAIN-P2", "CHAIN-P3", "CHAIN-P4"]


def _chain_page(records: list[tuple[str, str]], next_marker: str | None) -> bytes:
    page: dict = {"releases": [_ocds_release(o, d) for o, d in records]}
    if next_marker is not None:
        page["links"] = {"next": f"https://resume.invalid/{next_marker}"}
    return json.dumps(page).encode()


# marker-in-URL → page bytes. The fresh page-1 request (to the adapter's own
# base URL) matches no marker, so it falls through to the "" default below.
_NEWEST_FIRST_PAGES = {
    "": _chain_page(
        [("p1a", "2026-06-14T10:00:00Z"), ("p1b", "2026-06-13T10:00:00Z")],
        "CHAIN-P2",
    ),
    "CHAIN-P2": _chain_page(
        [("p2a", "2026-06-11T10:00:00Z"), ("p2b", "2026-06-10T10:00:00Z")],
        "CHAIN-P3",
    ),
    "CHAIN-P3": _chain_page(
        [("p3a", "2026-06-08T10:00:00Z"), ("p3b", "2026-06-07T10:00:00Z")],
        "CHAIN-P4",
    ),
    # Last page (oldest), no `links.next` — the end of the feed.
    "CHAIN-P4": _chain_page(
        [("p4a", "2026-06-05T10:00:00Z"), ("p4b", "2026-06-04T10:00:00Z")],
        None,
    ),
}


class _CursorChainTransport(httpx.AsyncBaseTransport):
    """Serves a cursor chain from a marker→bytes map, recording every fetched
    URL. BLOCKS (awaits forever, after signalling `reached`) on a request whose
    URL contains `block_marker` — the page-boundary await where the 900s
    cancellation lands. `block_marker=None` serves the whole chain."""

    def __init__(
        self,
        pages: dict[str, bytes],
        block_marker: str | None,
        reached: asyncio.Event,
        fetched: list[str],
    ) -> None:
        self._pages = pages
        self._block = block_marker
        self._reached = reached
        self._fetched = fetched

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self._fetched.append(url)
        if self._block and self._block in url:
            self._reached.set()
            await asyncio.sleep(3600)  # cancellation injected here
        for marker, body in self._pages.items():
            if marker and marker in url:
                return httpx.Response(
                    200, content=body, headers={"Content-Type": "application/json"}
                )
        return httpx.Response(
            200,
            content=self._pages[""],
            headers={"Content-Type": "application/json"},
        )


def _chain_adapter_factory(
    code: str,
    pages: dict[str, bytes],
    block_marker: str | None,
    reached: asyncio.Event,
    fetched: list[str],
):
    base = ContractsFinderAdapter if code == "CF" else FTSAdapter

    class _RealOverChain(base):  # type: ignore[valid-type,misc]
        def __init__(self) -> None:
            super().__init__(
                client=httpx.AsyncClient(
                    transport=_CursorChainTransport(
                        pages, block_marker, reached, fetched
                    )
                )
            )
            self._sleep = _instant_sleep  # type: ignore[assignment]

    return _RealOverChain


@pytest.mark.parametrize("code", ["CF", "FTS"])
@pytest.mark.asyncio
async def test_cancel_saves_next_cursor_and_next_run_resumes_from_it(code) -> None:
    """Inject the 900s cancellation in the page-2 fetch after page 1 commits:
    the SAVED resume cursor equals page 1's `links.next` (NOT null), and the
    next run starts its fetch FROM that cursor (page 2), not page 1. The feed
    is NEWEST-first, so the date-watermark stays frozen (null) throughout —
    proving the cursor, not the date, carried the resume. CF and FTS both."""
    _engine, factory = make_engine_and_session()
    with factory() as db:
        src = Source(code=code, name=code, base_url="x", enabled=True)
        db.add(src)
        db.commit()
        src_id = src.id

    # Run 1: serve page 1, block on the page-2 fetch, cancel there.
    reached = asyncio.Event()
    fetched1: list[str] = []
    cls1 = _chain_adapter_factory(
        code, _NEWEST_FIRST_PAGES, "CHAIN-P2", reached, fetched1
    )
    with (
        patch.dict(ADAPTERS_PATH, {code: cls1}, clear=False),
        _PATCH_UPSERT,
        _PATCH_PORTALS,
        factory() as db,
    ):
        source = db.get(Source, src_id)
        task = asyncio.create_task(poll_source(db, source))
        await asyncio.wait_for(reached.wait(), timeout=5.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    with factory() as db:
        source = db.get(Source, src_id)
        # The saved resume cursor is page 1's next cursor — NOT null.
        assert source.resume_cursor == "https://resume.invalid/CHAIN-P2"
        # The date-watermark FROZE (newest-first feed): on its own the loop
        # would not advance — the cursor is what survived.
        assert source.last_polled_at is None
        # Page 1's two records committed.
        refs = set(
            db.execute(
                select(Tender.source_ref).where(Tender.source_code == code)
            ).scalars()
        )
        assert refs == {"p1a", "p1b"}

    # Run 2: resumes FROM the saved cursor (page 2), serve the rest to the end.
    fetched2: list[str] = []
    cls2 = _chain_adapter_factory(code, _NEWEST_FIRST_PAGES, None, asyncio.Event(), fetched2)
    with (
        patch.dict(ADAPTERS_PATH, {code: cls2}, clear=False),
        _PATCH_UPSERT,
        _PATCH_PORTALS,
        factory() as db,
    ):
        source = db.get(Source, src_id)
        run = await poll_source(db, source)
        assert run.status == "ok"

    # The very first request of run 2 was the saved cursor (page 2), not the
    # backlog start — no re-walk from page 1.
    assert "CHAIN-P2" in fetched2[0]
    assert not any("CHAIN-P3" in u for u in fetched1)  # run 1 never reached it


@pytest.mark.parametrize("code", ["CF", "FTS"])
@pytest.mark.asyncio
async def test_newest_first_backlog_drains_across_cycles_without_skipping(code) -> None:
    """The whole point: a NEWEST-first backlog drains FORWARD across successive
    cancelled cycles — every page is fetched exactly once across the run, no
    page skipped, no record duplicated — and the final cycle reaches the end of
    the feed and completes `ok`, clearing the cursor and finally advancing the
    date-watermark to present. Throughout the drain last_polled_at stays frozen
    (newest-first), so the cursor is demonstrably what walks the backlog."""
    _engine, factory = make_engine_and_session()
    with factory() as db:
        src = Source(code=code, name=code, base_url="x", enabled=True)
        db.add(src)
        db.commit()
        src_id = src.id

    # Each cycle resumes from the saved cursor, fetches one page, then the
    # timeout cancels in the NEXT page's fetch — except the final cycle, which
    # reaches the last page (no further fetch) and completes cleanly.
    for block_marker in _CHAIN:  # blocks on P2, then P3, then P4...
        reached = asyncio.Event()
        fetched: list[str] = []
        cls = _chain_adapter_factory(
            code, _NEWEST_FIRST_PAGES, block_marker, reached, fetched
        )
        with (
            patch.dict(ADAPTERS_PATH, {code: cls}, clear=False),
            _PATCH_UPSERT,
            _PATCH_PORTALS,
            factory() as db,
        ):
            source = db.get(Source, src_id)
            task = asyncio.create_task(poll_source(db, source))
            await asyncio.wait_for(reached.wait(), timeout=5.0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        with factory() as db:
            source = db.get(Source, src_id)
            # Mid-drain the date-watermark stays frozen on the newest-first
            # feed — only the cursor advances.
            assert source.last_polled_at is None
            assert source.resume_cursor is not None

    # Final cycle: resume from the last saved cursor (page 4), reach end-of-feed.
    fetched: list[str] = []
    cls = _chain_adapter_factory(code, _NEWEST_FIRST_PAGES, None, asyncio.Event(), fetched)
    with (
        patch.dict(ADAPTERS_PATH, {code: cls}, clear=False),
        _PATCH_UPSERT,
        _PATCH_PORTALS,
        factory() as db,
    ):
        source = db.get(Source, src_id)
        run = await poll_source(db, source)
        assert run.status == "ok"

    with factory() as db:
        source = db.get(Source, src_id)
        # Every page's records present exactly once — full backlog drained,
        # nothing skipped, nothing duplicated (the unique upsert + idempotent
        # cursor walk).
        rows = list(
            db.execute(
                select(Tender.source_ref).where(Tender.source_code == code)
            ).scalars()
        )
        assert sorted(rows) == [
            "p1a", "p1b", "p2a", "p2b", "p3a", "p3b", "p4a", "p4b",
        ]
        assert len(rows) == len(set(rows))  # no duplicates
        # End of feed: cursor cleared, date-watermark finally advances to
        # present so the next run starts a fresh window.
        assert source.resume_cursor is None
        assert source.last_polled_at is not None


@pytest.mark.parametrize("code", ["CF", "FTS"])
@pytest.mark.asyncio
async def test_overlapping_refetch_from_cursor_creates_no_duplicates(code) -> None:
    """Idempotency: re-fetching pages whose records were ALREADY committed
    creates no duplicate tenders. Run 1 commits page 1 then cancels; run 2
    resumes the cursor to the end; run 3 starts FRESH (no cursor) and re-walks
    the whole chain — the maximal overlap, every record re-fetched. The
    (source_code, source_ref) upsert makes the cursor walk safe to overlap."""
    _engine, factory = make_engine_and_session()
    with factory() as db:
        src = Source(code=code, name=code, base_url="x", enabled=True)
        db.add(src)
        db.commit()
        src_id = src.id

    # Run 1: cancel in the page-2 fetch — cursor saved = page 2, page 1
    # records committed.
    reached = asyncio.Event()
    cls1 = _chain_adapter_factory(
        code, _NEWEST_FIRST_PAGES, "CHAIN-P2", reached, []
    )
    with (
        patch.dict(ADAPTERS_PATH, {code: cls1}, clear=False),
        _PATCH_UPSERT,
        _PATCH_PORTALS,
        factory() as db,
    ):
        source = db.get(Source, src_id)
        task = asyncio.create_task(poll_source(db, source))
        await asyncio.wait_for(reached.wait(), timeout=5.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    # Run 2 resumes from the saved cursor (page 2) to the end.
    cls2 = _chain_adapter_factory(code, _NEWEST_FIRST_PAGES, None, asyncio.Event(), [])
    with (
        patch.dict(ADAPTERS_PATH, {code: cls2}, clear=False),
        _PATCH_UPSERT,
        _PATCH_PORTALS,
        factory() as db,
    ):
        source = db.get(Source, src_id)
        assert (await poll_source(db, source)).status == "ok"

    # Run 3 with no cursor (fresh) re-walks the WHOLE chain from page 1 — every
    # record is re-fetched (the maximal overlap). Still no duplicates.
    cls3 = _chain_adapter_factory(code, _NEWEST_FIRST_PAGES, None, asyncio.Event(), [])
    with (
        patch.dict(ADAPTERS_PATH, {code: cls3}, clear=False),
        _PATCH_UPSERT,
        _PATCH_PORTALS,
        factory() as db,
    ):
        source = db.get(Source, src_id)
        assert (await poll_source(db, source)).status == "ok"

    with factory() as db:
        rows = list(
            db.execute(
                select(Tender.source_ref).where(Tender.source_code == code)
            ).scalars()
        )
        # All eight unique records, exactly once, despite the overlapping
        # re-fetches across three runs.
        assert sorted(rows) == [
            "p1a", "p1b", "p2a", "p2b", "p3a", "p3b", "p4a", "p4b",
        ]
        assert len(rows) == len(set(rows))
