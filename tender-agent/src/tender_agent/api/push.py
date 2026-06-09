"""Push subscription endpoints — subscribe / unsubscribe / vapid-public-key.

Subscriptions are OWNED by the authenticated account: subscribing requires auth
and records the caller's account on the subscription, so every notification can
be targeted to that user's devices only (no cross-account leakage).
"""
from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from tender_agent.api.deps import require_account
from tender_agent.config import settings
from tender_agent.db import get_db
from tender_agent.models import Account, PushSubscription
from tender_agent.schemas import (
    PushSubscriptionCreate,
    PushSubscriptionRead,
    PushUnsubscribe,
    VapidPublicKey,
)
from tender_agent.services.push import push_configured

router = APIRouter(prefix="/push", tags=["push"])


@router.get("/vapid-public-key", response_model=VapidPublicKey)
def get_vapid_public_key() -> VapidPublicKey:
    """Return the VAPID public key for the dashboard to use when subscribing.

    The private key is server-only and never exposed.
    """
    if not push_configured():
        raise HTTPException(status_code=503, detail="push not configured")
    return VapidPublicKey(
        public_key=settings.vapid_public_key, subject=settings.vapid_subject
    )


@router.post(
    "/subscriptions", response_model=PushSubscriptionRead, status_code=201
)
def subscribe(
    payload: PushSubscriptionCreate,
    request: Request,
    account: Account = Depends(require_account),
    db: Session = Depends(get_db),
) -> PushSubscriptionRead:
    """Upsert a browser PushSubscription by endpoint, OWNED by the caller.

    Recording the authenticated account is what makes per-user dispatch possible
    — a subscription with no known owner is exactly the bug this closes. If the
    same endpoint re-subscribes under a different account (shared device), the
    owner is reassigned to the current caller.
    """
    if not push_configured():
        raise HTTPException(status_code=503, detail="push not configured")

    user_agent = request.headers.get("user-agent")
    existing = db.execute(
        select(PushSubscription).where(PushSubscription.endpoint == payload.endpoint)
    ).scalar_one_or_none()

    if existing is None:
        sub = PushSubscription(
            account_id=account.id,
            endpoint=payload.endpoint,
            p256dh=payload.keys.p256dh,
            auth=payload.keys.auth,
            filter_profile_id=payload.filter_profile_id,
            user_agent=user_agent,
        )
        db.add(sub)
    else:
        # Browser may re-subscribe with rotated keys; treat as upsert and (re)bind
        # ownership to the current caller.
        existing.account_id = account.id
        existing.p256dh = payload.keys.p256dh
        existing.auth = payload.keys.auth
        existing.filter_profile_id = payload.filter_profile_id
        existing.user_agent = user_agent
        existing.last_used_at = datetime.now(UTC)
        sub = existing

    db.commit()
    db.refresh(sub)
    return sub


@router.delete("/subscriptions", status_code=204)
def unsubscribe(payload: PushUnsubscribe, db: Session = Depends(get_db)) -> None:
    sub = db.execute(
        select(PushSubscription).where(PushSubscription.endpoint == payload.endpoint)
    ).scalar_one_or_none()
    if sub is None:
        # Idempotent — already gone is success.
        return
    db.delete(sub)
    db.commit()
