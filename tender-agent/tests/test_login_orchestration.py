"""Orchestrator login-via-human flow with a fake bridge + fake login adapter.

Covers: bridge-down failure, the waiting_for_login wait loop, the
needs_user_confirmation pause, the confirm/resume path, and document
persistence. DB-backed via the savepoint fixture.
"""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from tender_agent.db import engine
from tender_agent.models import FetchTask, Portal, Tender, TenderDocumentFile
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


class FakeBridge:
    def __init__(self, *, available=True, login_sequence=None):
        self.available = available
        self.login_sequence = list(login_sequence or ["logged_in"])
        self.closed = False

    async def bridge_available(self):
        return self.available

    async def open_session(self, slug, start_url):
        return {"session_id": slug}

    async def wait_for_login(self, slug, pattern, login_url=None, timeout_seconds=600):
        status = self.login_sequence.pop(0) if self.login_sequence else "timeout"
        return {"status": status, "current_url": "x"}

    async def close_session(self, slug):
        self.closed = True
        return {"closed": True}


def _persisted_file(tmp_path: Path) -> DownloadedFile:
    data = b"%PDF login-flow"
    sha = hashlib.sha256(data).hexdigest()
    rel = f"login/{sha[:2]}/{sha}.pdf"
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return DownloadedFile(
        url="https://delta-esourcing.com/d.pdf",
        path=str(p),
        filename="d.pdf",
        bytes=len(data),
        content_type="application/pdf",
        sha256=sha,
        storage_key=rel,
    )


class FakeLoginAdapter(PortalAdapter):
    platform_slug = "delta_esourcing"
    requires_browser = False
    requires_login = True

    def __init__(
        self,
        *,
        auth_results,
        locate_status,
        download=None,
        register_status=RegisterStatus.success,
    ):
        # auth_results: list of bools consumed by is_authenticated
        self._auth = list(auth_results)
        self._locate_status = locate_status
        self._download = download or DownloadResult(status=DownloadStatus.nothing_available)
        self._register = register_status
        self.registered = False

    def matches_url(self, url):
        return "delta-esourcing.com" in url

    def login_url(self):
        return "https://delta-esourcing.com/login"

    async def is_authenticated(self, ctx):
        return self._auth.pop(0) if self._auth else True

    async def authenticate(self, ctx, creds):
        return AuthResult(status=AuthStatus.success)

    async def locate_tender(self, ctx, ref):
        return LocateResult(status=self._locate_status, tender_url="https://delta-esourcing.com/t/1")

    async def register_interest(self, ctx):
        self.registered = True
        return RegisterResult(status=self._register)

    async def download_documents(self, ctx, dest_dir):
        return self._download


def _seed(db: Session, ref="login-1") -> tuple[Tender, Portal]:
    now = datetime.now(UTC)
    t = Tender(source_code="FTS", source_ref=ref, title="Login tender",
               source_url="https://delta-esourcing.com/t/1",
               first_seen_at=now, last_seen_at=now)
    db.add(t)
    db.flush()
    # Unique subdomain: still matches the delta platform pattern
    # `(^|\.)delta-esourcing\.com$` but never collides with the discovered
    # delta-esourcing.com portal in a populated dev DB.
    p = Portal(domain=f"{ref}.delta-esourcing.com", display_name="Delta",
               first_seen_at=now, last_seen_at=now, created_at=now, updated_at=now)
    db.add(p)
    db.flush()
    return t, p


def _task(db: Session, t: Tender) -> str:
    task = FetchTask(task_id=f"task-{t.id}", tender_id=t.id, status="running",
                     created_at=datetime.now(UTC), updated_at=datetime.now(UTC))
    db.add(task)
    db.flush()
    return task.task_id


def _orch(bridge) -> PortalOrchestrator:
    return PortalOrchestrator(bridge=bridge, login_wait_cycle_seconds=1, login_wait_total_seconds=3)


# --- migration columns present -----------------------------------------


def test_fetch_task_login_columns(db: Session):
    t, _ = _seed(db, "cols-1")
    task = FetchTask(task_id="cols-task", tender_id=t.id, status="waiting_for_login",
                     login_url="https://x/login", bridge_session_slug="delta_esourcing",
                     waiting_since=datetime.now(UTC),
                     created_at=datetime.now(UTC), updated_at=datetime.now(UTC))
    db.add(task)
    db.flush()
    got = db.execute(select(FetchTask).where(FetchTask.task_id == "cols-task")).scalar_one()
    assert got.login_url == "https://x/login"
    assert got.bridge_session_slug == "delta_esourcing"
    assert got.waiting_since is not None


# --- bridge down --------------------------------------------------------


@pytest.mark.asyncio
async def test_bridge_down_fails_clearly(db: Session):
    t, p = _seed(db, "down-1")
    adapter = FakeLoginAdapter(auth_results=[True], locate_status=LocateStatus.found)
    res = await _orch(FakeBridge(available=False))._run_login_adapter(
        db, adapter, t, p, "delta_esourcing", "FakeLoginAdapter", "eduard", [], None, False
    )
    assert res.status == OrchestrationStatus.bridge_unavailable
    assert "start-bridge" in res.detail.lower()


# --- waiting-for-login loop --------------------------------------------


@pytest.mark.asyncio
async def test_waiting_for_login_then_success(db: Session, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "tender_agent.services.portal_orchestrator.settings.document_storage_dir",
        str(tmp_path),
    )
    t, p = _seed(db, "wait-1")
    task_id = _task(db, t)
    # not authed first check; bridge says timeout then logged_in; then locate found.
    adapter = FakeLoginAdapter(
        auth_results=[False, False],
        locate_status=LocateStatus.found,
        download=DownloadResult(status=DownloadStatus.complete, files=[_persisted_file(tmp_path)]),
    )
    bridge = FakeBridge(login_sequence=["timeout", "logged_in"])
    res = await _orch(bridge)._run_login_adapter(
        db, adapter, t, p, "delta_esourcing", "FakeLoginAdapter", "eduard", [], task_id, False
    )
    assert res.status == OrchestrationStatus.complete
    assert res.files_persisted == 1
    # Task passed through waiting_for_login.
    task = db.execute(select(FetchTask).where(FetchTask.task_id == task_id)).scalar_one()
    assert task.bridge_session_slug == "delta_esourcing"


@pytest.mark.asyncio
async def test_login_timeout_fails(db: Session):
    t, p = _seed(db, "timeout-1")
    task_id = _task(db, t)
    adapter = FakeLoginAdapter(
        auth_results=[False, False, False, False], locate_status=LocateStatus.found
    )
    bridge = FakeBridge(login_sequence=["timeout", "timeout", "timeout"])
    res = await _orch(bridge)._run_login_adapter(
        db, adapter, t, p, "delta_esourcing", "FakeLoginAdapter", "eduard", [], task_id, False
    )
    assert res.status == OrchestrationStatus.failed
    assert "retry" in res.detail.lower()


# --- needs_user_confirmation -------------------------------------------


@pytest.mark.asyncio
async def test_express_interest_pauses(db: Session):
    t, p = _seed(db, "ei-1")
    task_id = _task(db, t)
    adapter = FakeLoginAdapter(
        auth_results=[True], locate_status=LocateStatus.requires_interest_first
    )
    res = await _orch(FakeBridge())._run_login_adapter(
        db, adapter, t, p, "delta_esourcing", "FakeLoginAdapter", "eduard", [], task_id, False
    )
    assert res.status == OrchestrationStatus.needs_user_confirmation
    task = db.execute(select(FetchTask).where(FetchTask.task_id == task_id)).scalar_one()
    assert task.status == "needs_user_confirmation"
    assert not adapter.registered  # never auto-clicked


@pytest.mark.asyncio
async def test_confirm_resume_registers_and_downloads(db: Session, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "tender_agent.services.portal_orchestrator.settings.document_storage_dir",
        str(tmp_path),
    )
    t, p = _seed(db, "resume-1")
    task_id = _task(db, t)
    adapter = FakeLoginAdapter(
        auth_results=[True],
        locate_status=LocateStatus.found,
        download=DownloadResult(status=DownloadStatus.complete, files=[_persisted_file(tmp_path)]),
    )
    res = await _orch(FakeBridge())._run_login_adapter(
        db, adapter, t, p, "delta_esourcing", "FakeLoginAdapter", "eduard", [], task_id, True
    )
    assert res.status == OrchestrationStatus.complete
    assert adapter.registered is True
    rows = db.execute(
        select(TenderDocumentFile).where(TenderDocumentFile.tender_id == t.id)
    ).scalars().all()
    assert len(rows) == 1


# --- full routing through fetch_tender_documents -----------------------


@pytest.mark.asyncio
async def test_routing_selects_login_adapter(db: Session, monkeypatch):
    """A tender on a Delta-platform portal routes to the login path."""
    from tender_agent.models import PortalPlatform, PortalUrlSighting

    t, p = _seed(db, "route-1")
    # Give the portal the delta platform so _select_adapter picks the registered
    # Delta adapter (which is requires_login -> login path -> bridge check).
    platform = db.execute(
        select(PortalPlatform).where(PortalPlatform.slug == "delta_esourcing")
    ).scalar_one()
    p.platform_id = platform.id
    # A link sighting so _select_portal picks this portal for the tender.
    db.add(
        PortalUrlSighting(
            portal_id=p.id,
            tender_id=t.id,
            url="https://www.delta-esourcing.com/delta/tender/1",
            extracted_from="documents",
            sighting_type="document_link",
            extracted_at=datetime.now(UTC),
        )
    )
    db.flush()
    # Bridge down -> login path returns bridge_unavailable (proves we entered it).
    orch = PortalOrchestrator(bridge=FakeBridge(available=False))
    res = await orch.fetch_tender_documents(t.id, "eduard", db=db)
    assert res.status == OrchestrationStatus.bridge_unavailable
