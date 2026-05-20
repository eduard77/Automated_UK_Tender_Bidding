"""POST /tenders/{id}/fetch-documents — the 'Generate brief' entry point.

Chunk 2 ships the wiring + a stubbed async task. The PortalOrchestrator is
implemented and unit-tested, but the endpoint deliberately returns a stub
task rather than launching real browser automation — real per-platform
adapters (and turning this on in production) land in chunk 3.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from tender_agent.db import get_db
from tender_agent.models import Tender

router = APIRouter(prefix="/tenders", tags=["tenders"])


class FetchTask(BaseModel):
    task_id: str
    tender_id: int
    status: str
    detail: str | None = None
    created_at: str


# In-memory task registry. Adequate for the single-process desktop deployment;
# becomes a real queue in chunk 3.
_TASKS: dict[str, FetchTask] = {}


@router.post("/{tender_id}/fetch-documents", status_code=202, response_model=FetchTask)
def start_fetch_documents(
    tender_id: int, db: Session = Depends(get_db)
) -> FetchTask:
    tender = db.get(Tender, tender_id)
    if tender is None:
        raise HTTPException(status_code=404, detail="tender not found")
    task = FetchTask(
        task_id=uuid.uuid4().hex,
        tender_id=tender_id,
        status="queued",
        detail="stubbed — orchestrator runs in chunk 3",
        created_at=datetime.now(UTC).isoformat(),
    )
    _TASKS[task.task_id] = task
    return task


@router.get(
    "/{tender_id}/fetch-documents/{task_id}", response_model=FetchTask
)
def get_fetch_status(tender_id: int, task_id: str) -> FetchTask:
    task = _TASKS.get(task_id)
    if task is None or task.tender_id != tender_id:
        raise HTTPException(status_code=404, detail="task not found")
    return task
