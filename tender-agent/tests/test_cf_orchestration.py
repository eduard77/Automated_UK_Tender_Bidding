"""Orchestrator CF routing + document persistence (sha256 dedup / idempotency).

DB-backed via the savepoint fixture. Adapter network is avoided by monkeypatching
the CF adapter's download to a canned result (or testing helpers directly).
"""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from tender_agent.db import engine
from tender_agent.models import Portal, PortalUrlSighting, Tender, TenderDocumentFile
from tender_agent.services.portal_orchestrator import (
    OrchestrationStatus,
    PortalOrchestrator,
)
from tender_agent.services.portals.contracts_finder import ContractsFinderDirectAdapter
from tender_agent.services.portals.fallback import FallbackAdapter
from tender_agent.services.portals.results import (
    DownloadedFile,
    DownloadResult,
    DownloadStatus,
)

ASSET = "https://assets.publishing.service.gov.uk/media/x/itt.pdf"


@pytest.fixture()
def db() -> Session:
    connection = engine.connect()
    outer = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        outer.rollback()
        connection.close()


def _tender(db, *, source="CF", ref="cf-1", documents=None) -> Tender:
    now = datetime.now(UTC)
    t = Tender(
        source_code=source,
        source_ref=ref,
        title="t",
        documents=documents,
        first_seen_at=now,
        last_seen_at=now,
    )
    db.add(t)
    db.flush()
    return t


# --- adapter selection --------------------------------------------------


def test_select_adapter_cf_by_source(db):
    t = _tender(db, source="CF", ref="sel-1")
    adapter, slug = PortalOrchestrator()._select_adapter(t, [], None)
    assert isinstance(adapter, ContractsFinderDirectAdapter)
    assert slug == "contracts_finder_direct"


def test_select_adapter_cf_by_url(db):
    t = _tender(db, source="TEST", ref="sel-2")
    adapter, slug = PortalOrchestrator()._select_adapter(t, [ASSET], None)
    assert isinstance(adapter, ContractsFinderDirectAdapter)


def test_select_adapter_fallback(db):
    t = _tender(db, source="TEST", ref="sel-3")
    adapter, _ = PortalOrchestrator()._select_adapter(
        t, ["https://buyer-portal.example/x"], None
    )
    assert isinstance(adapter, FallbackAdapter)


# --- persistence + dedup ------------------------------------------------


def _write(tmp_path: Path, data: bytes) -> DownloadedFile:
    sha = hashlib.sha256(data).hexdigest()
    rel = f"999/{sha[:2]}/{sha}.pdf"
    target = tmp_path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return DownloadedFile(
        url=ASSET,
        path=str(target),
        filename="itt.pdf",
        bytes=len(data),
        content_type="application/pdf",
        sha256=sha,
        storage_key=rel,
    )


def test_persist_documents_dedup(db, tmp_path):
    t = _tender(db, ref="persist-1")
    f = _write(tmp_path, b"%PDF one")
    dl = DownloadResult(status=DownloadStatus.complete, files=[f])
    orch = PortalOrchestrator()
    n1 = orch._persist_documents(db, t, dl)
    n2 = orch._persist_documents(db, t, dl)  # idempotent
    assert n1 == 1
    assert n2 == 1
    rows = db.execute(
        select(TenderDocumentFile).where(TenderDocumentFile.tender_id == t.id)
    ).scalars().all()
    assert len(rows) == 1  # no duplicate
    assert rows[0].sha256 == f.sha256
    assert rows[0].download_status == "ok"


def test_persist_skips_files_without_sha(db, tmp_path):
    t = _tender(db, ref="persist-2")
    f = DownloadedFile(url="u", path="/x", filename="x", bytes=1)  # no sha256
    dl = DownloadResult(status=DownloadStatus.complete, files=[f])
    assert PortalOrchestrator()._persist_documents(db, t, dl) == 0


# --- end-to-end CF route (monkeypatched adapter download) ---------------


@pytest.mark.asyncio
async def test_cf_route_persists_and_idempotent(db, tmp_path, monkeypatch):
    t = _tender(
        db,
        source="CF",
        ref="e2e-1",
        documents=[{"url": ASSET, "title": "ITT", "format": "pdf"}],
    )
    # Portal + link sighting so candidate URLs include the asset. Unique
    # subdomain to avoid colliding with a real assets.publishing portal in a
    # populated dev DB (CF routing is by source/URL, not portal domain).
    portal = Portal(
        domain="e2e-1.assets.publishing.service.gov.uk",
        display_name="CF assets",
        first_seen_at=datetime.now(UTC),
        last_seen_at=datetime.now(UTC),
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    db.add(portal)
    db.flush()
    db.add(
        PortalUrlSighting(
            portal_id=portal.id,
            tender_id=t.id,
            url=ASSET,
            extracted_from="documents",
            sighting_type="document_link",
            extracted_at=datetime.now(UTC),
        )
    )
    db.flush()

    canned = _write(tmp_path, b"%PDF real")

    async def fake_download(self, ctx, dest_dir):
        return DownloadResult(status=DownloadStatus.complete, files=[canned])

    monkeypatch.setattr(
        ContractsFinderDirectAdapter, "download_documents", fake_download
    )

    orch = PortalOrchestrator()
    res = await orch.fetch_tender_documents(t.id, "eduard", db=db)
    assert res.status == OrchestrationStatus.complete
    assert res.adapter == "ContractsFinderDirectAdapter"
    assert res.files_persisted == 1
    rows = db.execute(
        select(TenderDocumentFile).where(TenderDocumentFile.tender_id == t.id)
    ).scalars().all()
    assert len(rows) == 1

    # Re-run: still one row (sha256 dedup).
    res2 = await orch.fetch_tender_documents(t.id, "eduard", db=db)
    assert res2.status == OrchestrationStatus.complete
    rows2 = db.execute(
        select(TenderDocumentFile).where(TenderDocumentFile.tender_id == t.id)
    ).scalars().all()
    assert len(rows2) == 1


@pytest.mark.asyncio
async def test_cf_empty_is_complete(db, monkeypatch):
    # CF tender, no documents, no sightings -> CF adapter -> complete empty.
    t = _tender(db, source="CF", ref="empty-1", documents=[])
    res = await PortalOrchestrator().fetch_tender_documents(t.id, "eduard", db=db)
    assert res.status == OrchestrationStatus.complete
    assert res.files_persisted == 0
    assert res.adapter == "ContractsFinderDirectAdapter"
