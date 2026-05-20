#!/usr/bin/env python3
"""Chunk-3 smoke test: drive the real fetch flow end to end.

1. Create (or reuse) a CF tender pointing at a real public assets.publishing
   PDF, plus the matching portal + sighting.
2. Run the PortalOrchestrator for real (no mocks) -> file on disk.
3. Print the persisted document rows + on-disk paths.

Run inside the app container so it shares the DB + storage dir.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqlalchemy import select  # noqa: E402

from tender_agent.config import settings  # noqa: E402
from tender_agent.db import SessionLocal  # noqa: E402
from tender_agent.models import (  # noqa: E402
    Portal,
    PortalUrlSighting,
    Tender,
    TenderDocumentFile,
)
from tender_agent.services.platform_matching import find_platform_for_domain  # noqa: E402
from tender_agent.services.portal_orchestrator import PortalOrchestrator  # noqa: E402

ASSET = (
    "https://assets.publishing.service.gov.uk/media/5a7898eced915d0422063dca/"
    "PPN5-11-Influencing-activity-on-modernisation-of-public-procurement-rules.pdf"
)
REF = "SMOKE-CF-ASSET-1"


def setup() -> int:
    with SessionLocal() as db:
        tender = db.execute(
            select(Tender).where(
                Tender.source_code == "CF", Tender.source_ref == REF
            )
        ).scalar_one_or_none()
        now = datetime.now(UTC)
        if tender is None:
            tender = Tender(
                source_code="CF",
                source_ref=REF,
                title="SMOKE: PPN 05/11 (real assets.publishing PDF)",
                documents=[{"url": ASSET, "title": "PPN 05/11", "format": "pdf"}],
                first_seen_at=now,
                last_seen_at=now,
            )
            db.add(tender)
            db.flush()
        domain = "assets.publishing.service.gov.uk"
        portal = db.execute(
            select(Portal).where(Portal.domain == domain)
        ).scalar_one_or_none()
        if portal is None:
            platform = find_platform_for_domain(db, domain)
            portal = Portal(
                domain=domain,
                display_name="Contracts Finder assets",
                platform_id=platform.id if platform else None,
                first_seen_at=now,
                last_seen_at=now,
                created_at=now,
                updated_at=now,
            )
            db.add(portal)
            db.flush()
        existing = db.execute(
            select(PortalUrlSighting).where(
                PortalUrlSighting.tender_id == tender.id,
                PortalUrlSighting.url == ASSET,
            )
        ).scalar_one_or_none()
        if existing is None:
            db.add(
                PortalUrlSighting(
                    portal_id=portal.id,
                    tender_id=tender.id,
                    url=ASSET,
                    extracted_from="documents",
                    sighting_type="document_link",
                    extracted_at=now,
                )
            )
        db.commit()
        return tender.id


async def run(tender_id: int) -> None:
    orch = PortalOrchestrator()
    result = await orch.fetch_tender_documents(tender_id, "eduard")
    print("\n=== ORCHESTRATION RESULT ===")
    print("status:", result.status.value)
    print("adapter:", result.adapter)
    print("files_persisted:", result.files_persisted)
    print("missing:", result.missing)

    with SessionLocal() as db:
        rows = db.execute(
            select(TenderDocumentFile).where(
                TenderDocumentFile.tender_id == tender_id
            )
        ).scalars().all()
        print("\n=== tender_document_files ===")
        for r in rows:
            path = Path(settings.document_storage_dir) / (r.storage_key or "")
            print(
                f"  id={r.id} status={r.download_status} bytes={r.bytes} "
                f"sha={ (r.sha256 or '')[:12] } exists={path.is_file()}"
            )
            print(f"     path={path}")


def main() -> int:
    tid = setup()
    print("tender_id:", tid)
    asyncio.run(run(tid))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
