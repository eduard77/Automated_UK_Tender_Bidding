"""FastAPI dependencies for the auth + entitlement layer.

`current_account` is the soft dependency — returns the Account or None for
anonymous callers. Used by /tenders/{id}/brief and the document endpoints so
the same handler can serve both anonymous (preview) and authenticated (full)
requests with the gating decided server-side.

`require_account` raises 401 — used by /me, /billing/*, /me/* etc.
"""
from __future__ import annotations

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from tender_agent.config import settings
from tender_agent.db import get_db
from tender_agent.models import Account
from tender_agent.services.accounts.auth import resolve_session


def current_account(
    request: Request, db: Session = Depends(get_db)
) -> Account | None:
    """Anonymous-friendly. Returns None when the session cookie is missing,
    invalid, or expired — callers fall back to the preview path."""
    token = request.cookies.get(settings.session_cookie_name)
    return resolve_session(db, token)


def require_account(
    account: Account | None = Depends(current_account),
) -> Account:
    if account is None:
        raise HTTPException(status_code=401, detail="authentication_required")
    return account
