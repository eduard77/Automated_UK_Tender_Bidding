"""SourceAdapter hardening primitives (2026-06-12 discovery outage).

Unit tests for the pieces the adapter fixes are built on:
  - PageProgressTracker: the confirm-then-advance partial watermark;
  - check_body_complete: truncated-body detection against Content-Length;
  - poll_source: a FAILED run persists the adapter's progress watermark.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import httpx
import pytest

from tender_agent.adapters.base import (
    PageProgressTracker,
    SourceAdapter,
    TruncatedResponseError,
)
from tender_agent.schemas import NormalisedTender
from tender_agent.services.ingestion import poll_source

T1 = datetime(2026, 6, 1, 10, 0, tzinfo=UTC)
T2 = datetime(2026, 6, 2, 10, 0, tzinfo=UTC)
T3 = datetime(2026, 6, 3, 10, 0, tzinfo=UTC)
T4 = datetime(2026, 6, 4, 10, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# PageProgressTracker
# ---------------------------------------------------------------------------


def test_tracker_advances_per_page_on_ascending_feed() -> None:
    """Every consumed page advances the watermark to the running max once the
    feed is known to ascend — the resume point stays in lockstep with the
    committed records, so a mid-sweep cancellation keeps each page's progress
    (the 2026-06-14 CF/FTS non-advancing loop)."""
    tracker = PageProgressTracker()
    tracker.page_done([T1, T2])  # page 1 ascends within itself → advance now
    assert tracker.watermark == T2
    tracker.page_done([T3, T4])  # page 2 confirms + advances further
    assert tracker.watermark == T4


def test_tracker_advances_on_a_single_ascending_page() -> None:
    """A window that fits in ONE page (no successor to confirm it) still
    advances — its own ascending record order proves the direction. The old
    confirm-with-next-page rule left this at None forever (clean runs that
    nonetheless never persisted a watermark)."""
    tracker = PageProgressTracker()
    tracker.page_done([T1, T2, T3])
    assert tracker.watermark == T3


def test_tracker_freezes_on_descending_feed() -> None:
    """Advancing on a newest-first feed would skip every older unfetched
    page — a descending page must freeze the tracker permanently."""
    # Detected intra-page (first page already descending).
    tracker = PageProgressTracker()
    tracker.page_done([T4, T3])  # newest-first within the first page → freeze
    assert tracker.watermark is None
    tracker.page_done([T1, T2])  # later ascending page changes nothing
    assert tracker.watermark is None

    # Detected cross-page (first page single-record, second page is older).
    tracker = PageProgressTracker()
    tracker.page_done([T4])  # page 1 (newest), no intra-page signal yet
    assert tracker.watermark is None
    tracker.page_done([T1])  # page 2 is OLDER → freeze
    assert tracker.watermark is None
    tracker.page_done([T3])
    assert tracker.watermark is None


def test_tracker_equal_boundary_counts_as_ascending() -> None:
    tracker = PageProgressTracker()
    tracker.page_done([T1])  # single record → direction unknown, holds
    assert tracker.watermark is None
    tracker.page_done([T2, T3])  # page 2 starts exactly at page 1's max → asc
    assert tracker.watermark == T3


def test_tracker_holds_until_direction_is_known() -> None:
    """A first page with a single record carries no intra-page signal — the
    tracker holds (does not guess) until a cross-page comparison settles the
    direction, then advances to the running max."""
    tracker = PageProgressTracker()
    tracker.page_done([T2])  # one record, no signal
    assert tracker.watermark is None
    tracker.page_done([T3, T4])  # T3 >= T2 → ascending confirmed
    assert tracker.watermark == T4


def test_tracker_tolerates_undated_pages() -> None:
    tracker = PageProgressTracker()
    tracker.page_done([])  # page with no parseable dates
    assert tracker.watermark is None
    tracker.page_done([T1, T2])
    tracker.page_done([T3, T4])
    assert tracker.watermark == T4


# ---------------------------------------------------------------------------
# check_body_complete
# ---------------------------------------------------------------------------


def _response(content: bytes, declared: str | None, encoding: str | None) -> httpx.Response:
    response = httpx.Response(200, content=content)
    if declared is not None:
        response.headers["Content-Length"] = declared
    if encoding is not None:
        response.headers["Content-Encoding"] = encoding
    return response


def test_short_body_raises_truncated() -> None:
    with pytest.raises(TruncatedResponseError):
        SourceAdapter.check_body_complete(_response(b"abc", "100", None))


def test_complete_body_passes() -> None:
    SourceAdapter.check_body_complete(_response(b"abc", "3", None))


def test_compressed_body_is_not_length_checked() -> None:
    # Content-Length counts COMPRESSED bytes; httpx hands us decompressed
    # content, so the check must skip non-identity encodings.
    SourceAdapter.check_body_complete(_response(b"abc", "100", "gzip"))


# ---------------------------------------------------------------------------
# poll_source persists partial progress on a FAILED run
# ---------------------------------------------------------------------------


PROGRESS = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)


class _PartialProgressAdapter(SourceAdapter):
    """Fails mid-run but reports confirmed-page progress — the CF 429 shape."""

    code = "FAKE"
    name = "Fake adapter"
    base_url = "https://example.invalid"

    async def fetch_since(
        self, since: datetime
    ) -> AsyncIterator[NormalisedTender]:
        self.progress_watermark = PROGRESS
        self.had_errors = True
        self.record_error("page 3: 429 after 4 attempts; aborting run")
        if False:  # pragma: no cover
            yield  # type: ignore[unreachable]


@pytest.mark.asyncio
async def test_failed_run_advances_watermark_to_confirmed_progress() -> None:
    """The 429-spiral breaker: a failed run keeps the progress its confirmed
    pages earned, so the retry's window SHRINKS instead of replaying the
    whole backlog (which is what re-triggered the rate limit)."""
    source = MagicMock()
    source.id = 42
    source.code = "FAKE"
    source.last_polled_at = None
    db = MagicMock()

    with patch.dict(
        "tender_agent.services.ingestion.ADAPTERS",
        {"FAKE": _PartialProgressAdapter},
        clear=False,
    ):
        run = await poll_source(db, source)

    assert run.status == "error"
    assert source.last_polled_at == PROGRESS  # partial progress persisted


@pytest.mark.asyncio
async def test_failed_run_never_moves_watermark_backwards() -> None:
    """Monotonic: an already-ahead watermark is never regressed by a
    failed run's older progress."""
    ahead = datetime(2026, 6, 11, 9, 0, tzinfo=UTC)
    source = MagicMock()
    source.id = 42
    source.code = "FAKE"
    source.last_polled_at = ahead
    db = MagicMock()

    with patch.dict(
        "tender_agent.services.ingestion.ADAPTERS",
        {"FAKE": _PartialProgressAdapter},
        clear=False,
    ):
        run = await poll_source(db, source)

    assert run.status == "error"
    assert source.last_polled_at == ahead  # untouched
