"""Accounts API — signup, login, logout, /me, dev overrides.

Search stays anonymous — none of the endpoints in this module are required
for the public discovery flow. /me is what the dashboard polls to decide how
to render brief / document panels (full vs. teaser).

Dev overrides are GUARDED — they refuse when `TENDER_AGENT_ENV=production`.
"""
from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from tender_agent.api.deps import current_account, require_account
from tender_agent.config import settings
from tender_agent.db import get_db
from tender_agent.models import Account
from tender_agent.services.accounts.auth import (
    AuthError,
    issue_session,
    revoke_session,
)
from tender_agent.services.accounts.auth import (
    login as login_service,
)
from tender_agent.services.accounts.auth import (
    signup as signup_service,
)
from tender_agent.services.accounts.entitlement import (
    PLAN_MONTHLY_LIMITS,
    grant_entitlement,
    is_plan_active,
)

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/accounts", tags=["accounts"])
me_router = APIRouter(tags=["accounts"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class SignupRequest(BaseModel):
    email: EmailStr
    password: str


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class AccountRead(BaseModel):
    id: int
    email: str
    plan: str
    # For plan_100 — the per-period budget. Null for unlimited and for
    # free/payg accounts (they don't have a generation budget).
    plan_limit: int | None
    plan_used: int
    is_plan_active: bool
    has_unlimited: bool


class FreeAlertSignupResponse(BaseModel):
    id: int
    email: str
    plan: str
    message: str


class DevOverrideRequest(BaseModel):
    account_id: int
    plan: str | None = None
    grant_tender_id: int | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=settings.session_cookie_name,
        value=token,
        max_age=settings.session_lifetime_days * 24 * 3600,
        httponly=True,
        secure=settings.tender_agent_env == "production",
        samesite="lax",
        path="/",
    )


def _account_read(account: Account) -> AccountRead:
    limit = PLAN_MONTHLY_LIMITS.get(account.plan)
    return AccountRead(
        id=account.id,
        email=account.email,
        plan=account.plan,
        plan_limit=limit,
        plan_used=account.brief_generations_this_period,
        is_plan_active=is_plan_active(account),
        has_unlimited=(
            account.plan == "plan_unlimited" and is_plan_active(account)
        ),
    )


def _is_production() -> bool:
    return settings.tender_agent_env == "production"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/signup", response_model=AccountRead)
def signup(
    body: SignupRequest,
    response: Response,
    db: Session = Depends(get_db),
) -> AccountRead:
    """Email + password signup. Creates a free-tier account; the user is
    automatically logged in (session cookie set on the response). For the
    free-alerts flow the dashboard hits /accounts/free-alerts instead — same
    underlying signup, but the response message reflects the intent."""
    try:
        account = signup_service(
            db, email=body.email, password=body.password
        )
    except AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    session = issue_session(db, account)
    _set_session_cookie(response, session.token)
    return _account_read(account)


@router.post("/login", response_model=AccountRead)
def login(
    body: LoginRequest,
    response: Response,
    db: Session = Depends(get_db),
) -> AccountRead:
    try:
        account = login_service(db, email=body.email, password=body.password)
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    session = issue_session(db, account)
    _set_session_cookie(response, session.token)
    return _account_read(account)


@router.post("/logout", status_code=204)
def logout(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> Response:
    token = request.cookies.get(settings.session_cookie_name)
    if token:
        revoke_session(db, token)
    response.delete_cookie(
        key=settings.session_cookie_name, path="/"
    )
    response.status_code = 204
    return response


@router.post("/free-alerts", response_model=FreeAlertSignupResponse)
def free_alerts_signup(
    body: SignupRequest,
    response: Response,
    db: Session = Depends(get_db),
) -> FreeAlertSignupResponse:
    """Free-tier signup specifically for the 'Get alerts' flow on the
    search page. Same DB path as /signup, different framing so the dashboard
    can show 'Alerts are on' messaging."""
    try:
        account = signup_service(db, email=body.email, password=body.password)
    except AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    session = issue_session(db, account)
    _set_session_cookie(response, session.token)
    return FreeAlertSignupResponse(
        id=account.id,
        email=account.email,
        plan=account.plan,
        message=(
            "Free alerts enabled. We'll notify you when new tenders match "
            "your filters."
        ),
    )


@me_router.get("/me", response_model=AccountRead | None)
def me(
    account: Account | None = Depends(current_account),
) -> AccountRead | None:
    """Anonymous callers get None back (200). The dashboard treats None as
    'show signup CTA'; otherwise it renders the plan + usage."""
    if account is None:
        return None
    return _account_read(account)


# ---------------------------------------------------------------------------
# Dev-only overrides — REFUSED IN PRODUCTION.
# Lets us test every gate before Stripe keys exist on the box.
# ---------------------------------------------------------------------------


@router.post("/dev/override", response_model=AccountRead)
def dev_override(
    body: DevOverrideRequest,
    _self: Account = Depends(require_account),
    db: Session = Depends(get_db),
) -> AccountRead:
    """Set an account's plan and/or grant an entitlement for a tender —
    bypasses Stripe entirely. Refused when TENDER_AGENT_ENV=production.

    Auth is still required (`require_account`) so a random anon can't poke
    other people's accounts; in dev we just need to be logged in.
    """
    if _is_production():
        raise HTTPException(
            status_code=403,
            detail="dev_overrides_disabled_in_production",
        )
    target = db.get(Account, body.account_id)
    if target is None:
        raise HTTPException(status_code=404, detail="account_not_found")

    if body.plan is not None:
        if body.plan not in {"free", "payg", "plan_100", "plan_unlimited"}:
            raise HTTPException(status_code=400, detail="invalid_plan")
        target.plan = body.plan
        if body.plan in PLAN_MONTHLY_LIMITS:
            from datetime import UTC, datetime, timedelta

            target.current_period_end = datetime.now(UTC) + timedelta(days=30)
            target.brief_generations_this_period = 0
        else:
            target.current_period_end = None
        db.commit()

    if body.grant_tender_id is not None:
        grant_entitlement(
            db,
            account=target,
            tender_id=body.grant_tender_id,
            source="dev",
        )

    return _account_read(target)
