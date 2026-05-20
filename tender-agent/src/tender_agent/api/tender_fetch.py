"""POST /tenders/{id}/fetch-documents and document file serving.

Chunk 3 makes this real: the endpoint records a durable FetchTask, kicks off
the PortalOrchestrator in the background, and the orchestrator persists the
fetched files as TenderDocumentFile rows. A second endpoint streams a stored
document's bytes back to the browser.
"""
from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from tender_agent.config import settings
from tender_agent.db import SessionLocal, get_db
from tender_agent.models import FetchTask, Tender, TenderDocumentFile
from tender_agent.services.portal_orchestrator import PortalOrchestrator
from tender_agent.services.portals.contracts_finder import secure_filename

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/tenders", tags=["tenders"])


class FetchTaskRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    task_id: str
    tender_id: int
    status: str
    adapter: str | None = None
    platform_slug: str | None = None
    files_count: int = 0
    missing_count: int = 0
    detail: str | None = None
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None = None


def _update_task(task_id: str, **fields) -> None:
    with SessionLocal() as db:
        task = db.execute(
            select(FetchTask).where(FetchTask.task_id == task_id)
        ).scalar_one_or_none()
        if task is None:
            return
        for k, v in fields.items():
            setattr(task, k, v)
        task.updated_at = datetime.now(UTC)
        db.commit()


async def schedule_fetch(task_id: str, tender_id: int) -> None:
    """Background runner. Tests monkeypatch this module attribute to a no-op so
    the TestClient never triggers a real orchestration / network call."""
    _update_task(task_id, status="running")
    orch = PortalOrchestrator()
    try:
        result = await orch.fetch_tender_documents(tender_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("fetch_task.failed", task_id=task_id, tender_id=tender_id)
        _update_task(
            task_id,
            status="error",
            detail=f"{type(exc).__name__}: {exc}",
            finished_at=datetime.now(UTC),
        )
        return
    _update_task(
        task_id,
        status=result.status.value,
        adapter=result.adapter,
        platform_slug=result.platform_slug,
        files_count=result.files_persisted,
        missing_count=len(result.missing),
        detail=result.detail,
        result={
            "status": result.status.value,
            "files": result.files,
            "missing": result.missing,
            "files_persisted": result.files_persisted,
        },
        finished_at=datetime.now(UTC),
    )


@router.post(
    "/{tender_id}/fetch-documents", status_code=202, response_model=FetchTaskRead
)
def start_fetch_documents(
    tender_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> FetchTaskRead:
    tender = db.get(Tender, tender_id)
    if tender is None:
        raise HTTPException(status_code=404, detail="tender not found")
    now = datetime.now(UTC)
    task = FetchTask(
        task_id=uuid.uuid4().hex,
        tender_id=tender_id,
        status="queued",
        created_at=now,
        updated_at=now,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    # Reference the bare name so tests can monkeypatch schedule_fetch.
    background_tasks.add_task(schedule_fetch, task.task_id, tender_id)
    return task


@router.get("/{tender_id}/fetch-documents/{task_id}", response_model=FetchTaskRead)
def get_fetch_status(
    tender_id: int, task_id: str, db: Session = Depends(get_db)
) -> FetchTaskRead:
    task = db.execute(
        select(FetchTask).where(FetchTask.task_id == task_id)
    ).scalar_one_or_none()
    if task is None or task.tender_id != tender_id:
        raise HTTPException(status_code=404, detail="task not found")
    return task


@router.get("/{tender_id}/documents/{doc_id}/file")
def serve_document_file(
    tender_id: int, doc_id: int, db: Session = Depends(get_db)
) -> FileResponse:
    doc = db.get(TenderDocumentFile, doc_id)
    if doc is None or doc.tender_id != tender_id:
        raise HTTPException(status_code=404, detail="document not found")
    if not doc.storage_key or doc.download_status != "ok":
        raise HTTPException(status_code=409, detail="document not downloaded")

    root = Path(settings.document_storage_dir).resolve()
    target = (root / doc.storage_key).resolve()
    # Path-traversal guard: the resolved file must live under the storage root.
    if not str(target).startswith(str(root) + os.sep):
        raise HTTPException(status_code=400, detail="invalid storage path")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="file missing on disk")

    download_name = secure_filename(doc.title or f"document-{doc.id}")
    media_type = _media_type(doc.format)
    return FileResponse(
        path=str(target),
        media_type=media_type,
        filename=download_name,
    )


def _media_type(fmt: str | None) -> str:
    if not fmt:
        return "application/octet-stream"
    f = fmt.lower()
    mapping = {
        "pdf": "application/pdf",
        "doc": "application/msword",
        "docx": (
            "application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document"
        ),
        "xls": "application/vnd.ms-excel",
        "xlsx": (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        "html": "text/html",
        "zip": "application/zip",
        "txt": "text/plain",
    }
    return mapping.get(f, "application/octet-stream")
