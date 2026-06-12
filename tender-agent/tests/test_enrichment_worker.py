"""Enrichment worker — decouples slow per-tender work from the poll cycle.

Phase-1 rev 3 (2026-06-11). The live Log stream proved the cause of
the EU_SUPPLY / ATAMIS starvation: `poll_source` was running
~10–15 s per-tender Anthropic extraction in-line on early-list
sources (FTS first, with every new tender matching a profile),
consuming the entire 30-min poll interval; the sources at the tail
of the list never got reached.

These tests pin three contracts that the fix has to satisfy:

  1. `poll_source` NO LONGER calls `extract_requirements` or
     `download_documents_for_tender` in the hot path — those imports
     are gone from ingestion.py and the matched-tender branch
     dispatches push only.
  2. `process_pending_enrichment` finds matched-but-unenriched
     tenders, respects the batch limit, skips already-enriched
     tenders, and skips non-matched ones.
  3. `enrich_tender` runs both halves (download + extract) and
     swallows exceptions in each independently so one failing half
     doesn't skip the other.

Pure SQLAlchemy + structlog capture; in-memory SQLite via the
billing fixtures helper.
"""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tender_agent.models import (
    FilterMatch,
    FilterProfile,
    Tender,
    TenderRequirements,
)
from tender_agent.services import enrichment_worker, ingestion
from tests._billing_fixtures import make_engine_and_session

NOW = datetime(2026, 6, 11, 12, 0, tzinfo=UTC)


@pytest.fixture()
def session_factory():
    _engine, factory = make_engine_and_session()
    return factory


def _make_profile(db) -> FilterProfile:
    profile = FilterProfile(name="all", enabled=True)
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


def _make_tender(db, *, ref: str, title: str = "Tender") -> Tender:
    tender = Tender(
        source_code="FTS",
        source_ref=ref,
        title=title,
        first_seen_at=NOW,
        last_seen_at=NOW,
    )
    db.add(tender)
    db.commit()
    db.refresh(tender)
    return tender


def _match(db, tender: Tender, profile: FilterProfile) -> None:
    db.add(
        FilterMatch(tender_id=tender.id, filter_profile_id=profile.id, score=1.0)
    )
    db.commit()


# ---------------------------------------------------------------------------
# (1) poll_source no longer enriches in-line
# ---------------------------------------------------------------------------


def test_ingestion_no_longer_imports_inline_enrichment() -> None:
    """The fix removes the per-tender download + extract from the hot
    path. If a future refactor accidentally re-imports them at the
    module level, this test catches it before the live logs do."""
    assert not hasattr(ingestion, "extract_requirements")
    assert not hasattr(ingestion, "download_documents_for_tender")
    assert not hasattr(ingestion, "_enrich_matched_tender")


# ---------------------------------------------------------------------------
# (2) queue selection — matched-and-unenriched, with FIFO + limit
# ---------------------------------------------------------------------------


def test_pending_tenders_picks_matched_unenriched_only(session_factory) -> None:
    with session_factory() as db:
        profile = _make_profile(db)

        matched_pending = _make_tender(db, ref="A", title="Matched, pending")
        _match(db, matched_pending, profile)

        matched_done = _make_tender(db, ref="B", title="Matched, enriched")
        _match(db, matched_done, profile)
        db.add(
            TenderRequirements(tender_id=matched_done.id, summary="already done")
        )
        db.commit()

        _make_tender(db, ref="C", title="Unmatched")  # no FilterMatch

        result = enrichment_worker._pending_tenders(db, limit=10)
        codes = [t.source_ref for t in result]
        assert codes == ["A"]


def test_pending_tenders_respects_limit_in_id_order(session_factory) -> None:
    with session_factory() as db:
        profile = _make_profile(db)
        ids: list[int] = []
        for i in range(5):
            t = _make_tender(db, ref=f"T-{i}")
            _match(db, t, profile)
            ids.append(t.id)
        result = enrichment_worker._pending_tenders(db, limit=3)
        assert [t.id for t in result] == ids[:3]


def test_pending_tenders_excludes_duplicates(session_factory) -> None:
    with session_factory() as db:
        profile = _make_profile(db)
        original = _make_tender(db, ref="ORIG")
        _match(db, original, profile)
        dup = _make_tender(db, ref="DUP")
        dup.duplicate_of_id = original.id
        db.commit()
        _match(db, dup, profile)
        result = enrichment_worker._pending_tenders(db, limit=10)
        assert [t.source_ref for t in result] == ["ORIG"]


# ---------------------------------------------------------------------------
# (3) process_pending_enrichment calls enrich for each, bounded by limit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_pending_enrichment_processes_batch(session_factory) -> None:
    with session_factory() as db:
        profile = _make_profile(db)
        for i in range(4):
            t = _make_tender(db, ref=f"R-{i}")
            _match(db, t, profile)

    seen: list[int] = []

    async def fake_enrich(_db, tender) -> None:
        seen.append(tender.id)

    with (
        session_factory() as db,
        patch.object(enrichment_worker, "enrich_tender", side_effect=fake_enrich),
    ):
        processed = await enrichment_worker.process_pending_enrichment(db, limit=3)

    assert processed == 3
    assert len(seen) == 3


@pytest.mark.asyncio
async def test_process_pending_enrichment_empty_queue(session_factory) -> None:
    """No matched tenders → zero processed, never raises."""
    with session_factory() as db:
        processed = await enrichment_worker.process_pending_enrichment(db, limit=5)
    assert processed == 0


# ---------------------------------------------------------------------------
# (4) enrich_tender — both halves run, both halves are independently
#     fault-tolerant (a flaky download must not skip extraction).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enrich_tender_runs_both_halves(session_factory) -> None:
    with session_factory() as db:
        profile = _make_profile(db)
        tender = _make_tender(db, ref="X")
        _match(db, tender, profile)

    download = AsyncMock(return_value=None)
    extract = MagicMock(return_value=None)
    with (
        patch.object(enrichment_worker, "download_documents_for_tender", new=download),
        patch.object(enrichment_worker, "extract_requirements", new=extract),
        session_factory() as db,
    ):
        # Re-fetch the tender on the new session so refresh() works.
        live = db.query(Tender).filter_by(source_ref="X").one()
        await enrichment_worker.enrich_tender(db, live)

    download.assert_awaited_once()
    extract.assert_called_once()


@pytest.mark.asyncio
async def test_enrich_tender_extract_runs_even_when_download_raises(
    session_factory,
) -> None:
    """A flaky downloader must not skip extraction; the two halves are
    independently wrapped on purpose."""
    with session_factory() as db:
        profile = _make_profile(db)
        tender = _make_tender(db, ref="Y")
        _match(db, tender, profile)

    download = AsyncMock(side_effect=RuntimeError("boom"))
    extract = MagicMock(return_value=None)
    with (
        patch.object(enrichment_worker, "download_documents_for_tender", new=download),
        patch.object(enrichment_worker, "extract_requirements", new=extract),
        session_factory() as db,
    ):
        live = db.query(Tender).filter_by(source_ref="Y").one()
        await enrichment_worker.enrich_tender(db, live)

    download.assert_awaited_once()
    extract.assert_called_once()
