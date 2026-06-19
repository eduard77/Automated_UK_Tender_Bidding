"""On-demand requirements extraction (cost fix, 2026-06-19).

docs/document-fetch-cost-audit.md found the recurring AI cost driver was the
per-matched-tender Sonnet requirements extraction the enrichment worker ran
AUTOMATICALLY — before any human looked at the tender. These tests pin the two
halves of the fix:

  1. Extraction NO LONGER runs automatically on match — `enrichment_worker_enabled`
     defaults off, and the scheduler job skips `process_pending_enrichment`.
  2. Extraction DOES run on-demand via POST /admin/extract-requirements/{id},
     exactly ONCE per tender: a second open serves the stored row without
     re-extracting (no re-charge).
"""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from tender_agent import scheduler
from tender_agent.api import admin
from tender_agent.config import Settings, settings
from tender_agent.db import get_db
from tender_agent.main import app
from tender_agent.models import Tender, TenderRequirements
from tests._billing_fixtures import make_engine_and_session

NOW = datetime(2026, 6, 19, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# (1) extraction does NOT run automatically on match
# ---------------------------------------------------------------------------


def test_enrichment_extraction_disabled_by_default() -> None:
    """The cost fix flips the default: automatic per-matched-tender extraction
    is OFF. Classification (the cheap Haiku pass) is a separate flag."""
    assert Settings().enrichment_worker_enabled is False


@pytest.mark.asyncio
async def test_worker_job_does_not_extract_when_disabled(monkeypatch) -> None:
    """With the worker disabled, the scheduler cycle never calls the Anthropic
    extraction path — the matched-tender Sonnet spend is gone from the hot
    loop. Classification is isolated off here so only the extraction branch is
    under test."""
    monkeypatch.setattr(settings, "enrichment_worker_enabled", False)
    monkeypatch.setattr(settings, "classifier_enabled", False)
    pending = AsyncMock()
    with patch.object(scheduler, "process_pending_enrichment", pending):
        await scheduler.enrichment_worker_job()
    pending.assert_not_awaited()


# ---------------------------------------------------------------------------
# (2) extraction runs on-demand exactly once, then serves the cached row
# ---------------------------------------------------------------------------


def test_on_demand_extracts_once_then_serves_cached(monkeypatch) -> None:
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    _engine, factory = make_engine_and_session()

    with factory() as db:
        tender = Tender(
            source_code="FTS",
            source_ref="on-demand-1",
            title="A tender",
            description="A real tender description with enough to analyse.",
            documents=[],  # no attachment URLs → download short-circuits
            first_seen_at=NOW,
            last_seen_at=NOW,
        )
        db.add(tender)
        db.commit()
        tender_id = tender.id

    def override() -> object:
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override

    calls = {"extract": 0}

    def fake_extract(db, tender):
        calls["extract"] += 1
        record = TenderRequirements(
            tender_id=tender.id, summary="on-demand brief", recommendation="review"
        )
        db.add(record)
        db.commit()
        db.refresh(record)
        return record

    download = AsyncMock(return_value=[])
    try:
        with (
            patch.object(admin, "extract_requirements", side_effect=fake_extract),
            patch.object(admin, "download_documents_for_tender", new=download),
        ):
            client = TestClient(app)
            first = client.post(f"/admin/extract-requirements/{tender_id}")
            second = client.post(f"/admin/extract-requirements/{tender_id}")
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert first.status_code == 200
    assert second.status_code == 200
    # Extraction ran exactly once; the second open was served from the stored
    # row — no second Anthropic call, no second charge.
    assert calls["extract"] == 1
    # And the documents were fetched only on that first (uncached) extraction.
    download.assert_awaited_once()
    assert first.json()["tender_id"] == tender_id
    assert second.json()["id"] == first.json()["id"]


def test_on_demand_force_reextracts(monkeypatch) -> None:
    """Operators can still force a re-run (overwrites + re-charges) — the
    `force=true` escape hatch keeps the old re-investigate behaviour."""
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    _engine, factory = make_engine_and_session()

    with factory() as db:
        tender = Tender(
            source_code="FTS",
            source_ref="on-demand-2",
            title="A tender",
            description="A real tender description.",
            documents=[],
            first_seen_at=NOW,
            last_seen_at=NOW,
        )
        db.add(tender)
        db.flush()  # assign tender.id before the FK below
        db.add(TenderRequirements(tender_id=tender.id, summary="stale"))
        db.commit()
        tender_id = tender.id

    def override() -> object:
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override

    calls = {"extract": 0}

    def fake_extract(db, tender):
        calls["extract"] += 1
        record = (
            db.query(TenderRequirements)
            .filter_by(tender_id=tender.id)
            .one_or_none()
        ) or TenderRequirements(tender_id=tender.id)
        record.summary = "fresh"
        db.add(record)
        db.commit()
        db.refresh(record)
        return record

    download = AsyncMock(return_value=[])
    try:
        with (
            patch.object(admin, "extract_requirements", side_effect=fake_extract),
            patch.object(admin, "download_documents_for_tender", new=download),
        ):
            client = TestClient(app)
            cached = client.post(f"/admin/extract-requirements/{tender_id}")
            forced = client.post(
                f"/admin/extract-requirements/{tender_id}?force=true"
            )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert cached.json()["summary"] == "stale"  # idempotent default
    assert forced.json()["summary"] == "fresh"  # force re-ran the extractor
    assert calls["extract"] == 1  # only the forced call extracted
