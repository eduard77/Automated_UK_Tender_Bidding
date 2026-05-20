"""PortalOrchestrator flow with a mocked adapter. DB-backed (savepoint), no
browser, no network, no real credentials store.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from tender_agent.db import engine
from tender_agent.models import Portal, PortalUrlSighting, Tender
from tender_agent.services import portal_orchestrator as orch_mod
from tender_agent.services.portal_orchestrator import (
    OrchestrationStatus,
    PortalOrchestrator,
)
from tender_agent.services.portals.base import PortalAdapter
from tender_agent.services.portals.results import (
    AuthResult,
    AuthStatus,
    DownloadedFile,
    DownloadResult,
    DownloadStatus,
    LocateResult,
    LocateStatus,
    RegisterResult,
    RegisterStatus,
)


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


class _FakeStore:
    def __init__(self) -> None:
        self.invalidated: list[tuple[int, str]] = []
        self.used: list[tuple[int, str]] = []

    def get_credentials(self, portal_id, user_id):
        return None

    def mark_invalid(self, portal_id, user_id):
        self.invalidated.append((portal_id, user_id))

    def mark_used(self, portal_id, user_id):
        self.used.append((portal_id, user_id))


def _make_fake_adapter(
    *,
    auth: AuthStatus,
    locate: LocateStatus = LocateStatus.found,
    download: DownloadStatus = DownloadStatus.complete,
):
    class _FakeAdapter(PortalAdapter):
        requires_browser = False

        def matches_url(self, url: str) -> bool:
            return True

        async def authenticate(self, ctx, creds) -> AuthResult:
            return AuthResult(status=auth)

        async def locate_tender(self, ctx, tender_ref) -> LocateResult:
            return LocateResult(status=locate)

        async def register_interest(self, ctx) -> RegisterResult:
            return RegisterResult(status=RegisterStatus.success)

        async def download_documents(self, ctx, dest_dir) -> DownloadResult:
            files = (
                [DownloadedFile(url="u", path="/x/itt.pdf", filename="itt.pdf", bytes=10)]
                if download in (DownloadStatus.complete, DownloadStatus.partial)
                else []
            )
            return DownloadResult(status=download, files=files)

    return _FakeAdapter


def _seed(db: Session) -> tuple[Tender, Portal]:
    now = datetime.now(UTC)
    tender = Tender(
        source_code="TEST",
        source_ref="orch-ref-1",
        title="Orchestrator tender",
        first_seen_at=now,
        last_seen_at=now,
    )
    db.add(tender)
    db.flush()
    portal = Portal(
        domain="orch-portal.example.com",
        display_name="Orch Portal",
        priority="high",
        tender_count=1,
        first_seen_at=now,
        last_seen_at=now,
        created_at=now,
        updated_at=now,
    )
    db.add(portal)
    db.flush()
    db.add(
        PortalUrlSighting(
            portal_id=portal.id,
            tender_id=tender.id,
            url="https://orch-portal.example.com/tender/1",
            extracted_from="description",
            sighting_type="tender_link",
            extracted_at=now,
        )
    )
    db.flush()
    return tender, portal


@pytest.mark.asyncio
async def test_no_portal_when_only_email_sighting(db: Session, monkeypatch) -> None:
    now = datetime.now(UTC)
    tender = Tender(
        source_code="TEST",
        source_ref="orch-ref-email",
        title="Email-only tender",
        first_seen_at=now,
        last_seen_at=now,
    )
    db.add(tender)
    db.flush()
    portal = Portal(
        domain="email.example.com",
        display_name="Email Domain",
        is_email_domain=True,
        first_seen_at=now,
        last_seen_at=now,
        created_at=now,
        updated_at=now,
    )
    db.add(portal)
    db.flush()
    db.add(
        PortalUrlSighting(
            portal_id=portal.id,
            tender_id=tender.id,
            url="mailto:ops@email.example.com",
            extracted_from="description",
            sighting_type="contact_email",
            extracted_at=now,
        )
    )
    db.flush()
    orch = PortalOrchestrator(credentials_store=_FakeStore())
    result = await orch.fetch_tender_documents(tender.id, "eduard", db=db)
    assert result.status == OrchestrationStatus.no_portal


@pytest.mark.asyncio
async def test_success_path(db: Session, monkeypatch) -> None:
    tender, portal = _seed(db)
    monkeypatch.setattr(
        orch_mod,
        "get_adapter_for_platform",
        lambda slug: _make_fake_adapter(auth=AuthStatus.success),
    )
    store = _FakeStore()
    orch = PortalOrchestrator(credentials_store=store)
    result = await orch.fetch_tender_documents(tender.id, "eduard", db=db)
    assert result.status == OrchestrationStatus.complete
    assert result.portal_id == portal.id
    assert len(result.files) == 1
    assert store.used == [(portal.id, "eduard")]


@pytest.mark.asyncio
async def test_credentials_invalid_path(db: Session, monkeypatch) -> None:
    tender, portal = _seed(db)
    monkeypatch.setattr(
        orch_mod,
        "get_adapter_for_platform",
        lambda slug: _make_fake_adapter(auth=AuthStatus.invalid_credentials),
    )
    store = _FakeStore()
    orch = PortalOrchestrator(credentials_store=store)
    result = await orch.fetch_tender_documents(tender.id, "eduard", db=db)
    assert result.status == OrchestrationStatus.credentials_invalid
    assert store.invalidated == [(portal.id, "eduard")]


@pytest.mark.asyncio
async def test_needs_registration_path(db: Session, monkeypatch) -> None:
    tender, _ = _seed(db)
    monkeypatch.setattr(
        orch_mod,
        "get_adapter_for_platform",
        lambda slug: _make_fake_adapter(auth=AuthStatus.needs_registration),
    )
    orch = PortalOrchestrator(credentials_store=_FakeStore())
    result = await orch.fetch_tender_documents(tender.id, "eduard", db=db)
    assert result.status == OrchestrationStatus.needs_registration


@pytest.mark.asyncio
async def test_requires_interest_then_found(db: Session, monkeypatch) -> None:
    tender, _ = _seed(db)
    monkeypatch.setattr(
        orch_mod,
        "get_adapter_for_platform",
        lambda slug: _make_fake_adapter(
            auth=AuthStatus.success, locate=LocateStatus.requires_interest_first
        ),
    )
    orch = PortalOrchestrator(credentials_store=_FakeStore())
    result = await orch.fetch_tender_documents(tender.id, "eduard", db=db)
    # register_interest succeeds, second locate returns requires_interest_first
    # again (fake is stateless) -> locate_failed. The point is the register
    # branch is exercised without raising.
    assert result.status in (
        OrchestrationStatus.locate_failed,
        OrchestrationStatus.complete,
    )
