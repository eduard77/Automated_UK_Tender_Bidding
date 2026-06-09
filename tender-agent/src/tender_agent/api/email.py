"""Email integration API: connect an inbox, manage connections, view filed mail.

Every endpoint is scoped to the authenticated Account — a connected inbox and
its tokens belong to one user; one user's email is never visible to another.
This is the first genuinely multi-user-shaped surface in the app.

The connect flow is OAuth read-only:
  POST /email/connect/{provider}  -> { authorization_url }  (user is redirected)
  GET  /email/oauth/callback       -> exchanges the code, stores the token,
                                       redirects back to the dashboard.

There is NO send endpoint anywhere — the system suggests a draft; the user
sends it themselves from their own mail client.
"""
from __future__ import annotations

import secrets
from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from tender_agent.api.deps import require_account
from tender_agent.api.schemas.email import (
    ConnectResponse,
    MailboxConnectionRead,
    MailboxMessageRead,
    PollNowResponse,
    ProviderStatus,
)
from tender_agent.config import settings
from tender_agent.db import get_db
from tender_agent.models import Account, MailboxAccount, MailboxMessage
from tender_agent.services.email.poller import poll_mailbox
from tender_agent.services.email.providers import (
    PROVIDER_NAMES,
    build_provider,
    canonical_provider,
    provider_configured,
)
from tender_agent.services.email.providers.base import ProviderNotConfiguredError
from tender_agent.services.email.token_store import encode_tokens

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/email", tags=["email"])

# Yahoo is deferred — see services/email/providers/yahoo.py.
_IMPLEMENTED = {"gmail", "outlook"}


@router.get("/providers", response_model=list[ProviderStatus])
def list_providers() -> list[ProviderStatus]:
    """Report which providers exist and which are configured (operator setup
    done). The dashboard uses this to enable/disable connect buttons."""
    return [
        ProviderStatus(
            provider=name,
            configured=provider_configured(name),
            implemented=name in _IMPLEMENTED,
        )
        for name in PROVIDER_NAMES
    ]


@router.post("/connect/{provider}", response_model=ConnectResponse)
def connect(
    provider: str,
    account: Account = Depends(require_account),
    db: Session = Depends(get_db),
) -> ConnectResponse:
    """Start the OAuth connect flow: create a pending mailbox row with a random
    state nonce and return the provider's consent URL."""
    name = canonical_provider(provider)
    if name not in PROVIDER_NAMES:
        raise HTTPException(status_code=404, detail="unknown provider")
    if not provider_configured(name):
        # Clear, non-cryptic message until the operator finishes setup.
        raise HTTPException(
            status_code=503,
            detail=f"{name} is not configured yet — provider OAuth app "
            "credentials are not set on the server",
        )

    # Drop any stale pending connection for this (account, provider) so the
    # nullable-email unique slot stays clean.
    for stale in db.execute(
        select(MailboxAccount)
        .where(MailboxAccount.account_id == account.id)
        .where(MailboxAccount.provider == name)
        .where(MailboxAccount.status == "pending")
    ).scalars():
        db.delete(stale)

    state = secrets.token_urlsafe(24)
    db.add(
        MailboxAccount(
            account_id=account.id,
            provider=name,
            status="pending",
            connect_state=state,
        )
    )
    db.commit()

    auth_url = build_provider(name).authorization_url(state=state)
    logger.info("email.connect_started", account_id=account.id, provider=name)
    return ConnectResponse(provider=name, authorization_url=auth_url)


@router.get("/oauth/callback")
async def oauth_callback(
    state: str = Query(...),
    code: str | None = Query(default=None),
    error: str | None = Query(default=None),
    account: Account = Depends(require_account),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """OAuth redirect target. Exchanges the code for tokens, learns the mailbox
    address, stores the encrypted token, and redirects back to the dashboard."""
    dash = settings.dashboard_base_url.rstrip("/")

    pending = db.execute(
        select(MailboxAccount)
        .where(MailboxAccount.connect_state == state)
        .where(MailboxAccount.account_id == account.id)
        .where(MailboxAccount.status == "pending")
    ).scalar_one_or_none()
    if pending is None:
        # Unknown/replayed state, or belongs to another account — refuse.
        raise HTTPException(status_code=400, detail="invalid or expired state")

    if error or not code:
        db.delete(pending)
        db.commit()
        return RedirectResponse(
            url=f"{dash}/settings/email?error={error or 'cancelled'}",
            status_code=303,
        )

    provider = build_provider(pending.provider)
    try:
        tokens = await provider.exchange_code(code)
        address = await provider.get_address(tokens)
    except ProviderNotConfiguredError as exc:
        db.delete(pending)
        db.commit()
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "email.oauth_exchange_failed",
            account_id=account.id,
            provider=pending.provider,
            error=str(exc),
        )
        db.delete(pending)
        db.commit()
        return RedirectResponse(
            url=f"{dash}/settings/email?error=connect_failed", status_code=303
        )

    # If this mailbox was connected before, update that row and drop the pending
    # one so the (account, provider, address) unique slot never collides.
    existing = db.execute(
        select(MailboxAccount)
        .where(MailboxAccount.account_id == account.id)
        .where(MailboxAccount.provider == pending.provider)
        .where(MailboxAccount.email_address == address)
        .where(MailboxAccount.id != pending.id)
    ).scalar_one_or_none()

    target = existing or pending
    target.email_address = address
    target.token_ciphertext = encode_tokens(tokens)
    target.status = "connected"
    target.connect_state = None
    target.last_error = None
    target.updated_at = datetime.now(UTC)
    if existing is not None:
        db.delete(pending)
    db.commit()

    logger.info(
        "email.connected", account_id=account.id, provider=pending.provider
    )
    return RedirectResponse(
        url=f"{dash}/settings/email?connected={pending.provider}",
        status_code=303,
    )


@router.get("/connections", response_model=list[MailboxConnectionRead])
def list_connections(
    account: Account = Depends(require_account),
    db: Session = Depends(get_db),
) -> list[MailboxAccount]:
    """List this account's mailbox connections (no secrets)."""
    return list(
        db.execute(
            select(MailboxAccount)
            .where(MailboxAccount.account_id == account.id)
            .where(MailboxAccount.status != "pending")
            .order_by(MailboxAccount.created_at.desc())
        )
        .scalars()
        .all()
    )


def _owned_mailbox(
    db: Session, mailbox_id: int, account: Account
) -> MailboxAccount:
    mailbox = db.get(MailboxAccount, mailbox_id)
    if mailbox is None or mailbox.account_id != account.id:
        # 404 (not 403) so we never confirm another account's mailbox exists.
        raise HTTPException(status_code=404, detail="connection not found")
    return mailbox


@router.delete("/connections/{mailbox_id}", status_code=204)
def disconnect(
    mailbox_id: int,
    account: Account = Depends(require_account),
    db: Session = Depends(get_db),
) -> None:
    """Disconnect a mailbox: revoke locally by dropping the encrypted token and
    marking it disconnected. The row is retained so its filed messages remain
    visible."""
    mailbox = _owned_mailbox(db, mailbox_id, account)
    mailbox.token_ciphertext = None
    mailbox.status = "disconnected"
    mailbox.connect_state = None
    mailbox.updated_at = datetime.now(UTC)
    db.commit()


@router.get(
    "/connections/{mailbox_id}/messages",
    response_model=list[MailboxMessageRead],
)
def list_messages(
    mailbox_id: int,
    account: Account = Depends(require_account),
    db: Session = Depends(get_db),
) -> list[MailboxMessage]:
    """List the filed (matched) messages for one of this account's mailboxes."""
    mailbox = _owned_mailbox(db, mailbox_id, account)
    return list(
        db.execute(
            select(MailboxMessage)
            .where(MailboxMessage.mailbox_account_id == mailbox.id)
            .order_by(MailboxMessage.created_at.desc())
        )
        .scalars()
        .all()
    )


@router.post(
    "/connections/{mailbox_id}/poll", response_model=PollNowResponse
)
async def poll_now(
    mailbox_id: int,
    account: Account = Depends(require_account),
    db: Session = Depends(get_db),
) -> PollNowResponse:
    """Manually trigger a poll for one mailbox — used by the operator to verify
    the connection end-to-end (send yourself a test email with a known tender
    reference in the subject, then poll). The autonomous path is the scheduled
    job; this is a convenience for that first real test."""
    mailbox = _owned_mailbox(db, mailbox_id, account)
    if mailbox.status != "connected":
        raise HTTPException(status_code=409, detail="mailbox not connected")
    summary = await poll_mailbox(db, mailbox)
    return PollNowResponse(
        mailbox_id=summary.mailbox_id,
        listed=summary.listed,
        filed=summary.filed,
        skipped_existing=summary.skipped_existing,
        no_match=summary.no_match,
        errors=summary.errors,
    )
