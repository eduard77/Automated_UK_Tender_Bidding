"""Operator-only admin endpoints for portal session management (login model B).

    POST /admin/portals/delta/session         — upload a Playwright storage_state
    GET  /admin/portals/delta/session/status  — is a session present? (no values)
    POST /admin/portals/delta/session/test    — probe whether it's actually logged in

All three are gated behind `require_account` (operator/account auth) at the
router level, so an anonymous or unauthenticated caller is rejected with 401
before the handler body runs. The uploaded storage_state is treated as a
credential: never logged, never returned. See
`services/portals/delta_session.py` for the model-B rationale and the manual
capture steps.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from tender_agent.api.deps import require_account
from tender_agent.services.portals import delta_session

router = APIRouter(
    prefix="/admin/portals",
    tags=["admin"],
    dependencies=[Depends(require_account)],
)


@router.post("/delta/session")
async def upload_delta_session(request: Request) -> dict:
    """Accept the raw contents of a Playwright `storage_state.json` as the
    request body (Content-Type: application/json), validate it's well-formed,
    and write it into the Delta slug's persistent-context dir, overwriting any
    existing one. Returns ok + the write timestamp.

    Raw body (not multipart) keeps this dependency-free — capture works with a
    plain `curl --data-binary @storage_state.json`.
    """
    raw = await request.body()
    if not raw:
        raise HTTPException(
            status_code=400,
            detail="empty body — POST the storage_state.json contents",
        )
    try:
        state = delta_session.validate_storage_state(raw)
    except delta_session.StorageStateValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    ts = delta_session.write_storage_state(state)
    return {
        "ok": True,
        "slug": delta_session.DELTA_SLUG,
        "updated_at": ts.isoformat(),
    }


@router.get("/delta/session/status")
def delta_session_status() -> dict:
    """Whether an uploaded session is present, plus a count + coarse heuristic.
    Never returns cookie names or values."""
    return delta_session.session_status()


@router.post("/delta/session/test")
async def test_delta_session() -> dict:
    """Run the existing `is_authenticated` probe headless against the uploaded
    session and report logged-in true/false. Does not click or register."""
    return await delta_session.test_session()
