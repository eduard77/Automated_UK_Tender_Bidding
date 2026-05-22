"""Web Push dispatch.

Best-effort. A failure here MUST NOT break ingestion or any caller — we always
log structured events and swallow exceptions at the dispatch boundary.

Subscriptions whose endpoint returns 404/410 ("gone") are deleted from the DB.
Successful sends update last_used_at.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime

import structlog
from pywebpush import WebPushException, webpush
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from tender_agent.config import settings
from tender_agent.models import PushSubscription, Tender

logger = structlog.get_logger(__name__)

DEAD_ENDPOINT_STATUSES = {404, 410}


def push_configured() -> bool:
    """True iff VAPID keys are set in the environment."""
    return bool(settings.vapid_public_key and settings.vapid_private_key)


def _build_tender_match_payload(tender: Tender) -> dict[str, str]:
    """Notification body for a "new tender matched a filter" event."""
    base = settings.dashboard_base_url.rstrip("/")
    title = "New tender match"
    body = f"{tender.title} — {tender.buyer_name}" if tender.buyer_name else tender.title
    return {
        "title": title,
        "body": body,
        "url": f"{base}/tenders/{tender.id}",
        "tag": f"match-{tender.id}",
    }


def _send_one(
    db: Session, subscription: PushSubscription, payload: dict[str, str]
) -> bool:
    """Dispatch one notification. Returns True on success.

    Mutates DB on side-effects (delete on dead endpoint, last_used_at on success)
    but does NOT commit — the caller is responsible for batching commits.
    """
    try:
        webpush(
            subscription_info={
                "endpoint": subscription.endpoint,
                "keys": {"p256dh": subscription.p256dh, "auth": subscription.auth},
            },
            data=json.dumps(payload),
            vapid_private_key=settings.vapid_private_key,
            vapid_claims={"sub": settings.vapid_subject},
        )
    except WebPushException as exc:
        status = exc.response.status_code if exc.response is not None else None
        if status in DEAD_ENDPOINT_STATUSES:
            logger.info(
                "push.endpoint_gone",
                subscription_id=subscription.id,
                status=status,
            )
            db.delete(subscription)
            return False
        logger.warning(
            "push.send_failed",
            subscription_id=subscription.id,
            status=status,
            error=str(exc),
        )
        return False
    except Exception as exc:  # noqa: BLE001 — last-resort net for non-WebPush errors
        logger.exception(
            "push.send_error",
            subscription_id=subscription.id,
            error=str(exc),
        )
        return False
    subscription.last_used_at = datetime.now(UTC)
    return True


def send_to_subscribers(
    db: Session, filter_profile_id: int, payload: dict[str, str]
) -> tuple[int, int]:
    """Dispatch `payload` to every subscriber of `filter_profile_id` plus every
    subscriber with `filter_profile_id IS NULL` (catch-all subscribers).

    Returns (sent, failed). Does NOT raise. Caller commits.
    """
    if not push_configured():
        logger.info("push.skip_unconfigured", filter_profile_id=filter_profile_id)
        return (0, 0)

    subs = list(
        db.execute(
            select(PushSubscription).where(
                or_(
                    PushSubscription.filter_profile_id == filter_profile_id,
                    PushSubscription.filter_profile_id.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )

    if not subs:
        logger.info("push.no_subscribers", filter_profile_id=filter_profile_id)
        return (0, 0)

    sent = failed = 0
    for sub in subs:
        if _send_one(db, sub, payload):
            sent += 1
        else:
            failed += 1

    logger.info(
        "push.dispatch_complete",
        filter_profile_id=filter_profile_id,
        sent=sent,
        failed=failed,
        total=len(subs),
    )
    return (sent, failed)


def send_system_notification(
    db: Session, title: str, body: str, url: str, tag: str | None = None
) -> None:
    """Dispatch a system notification (e.g. 'log in to fetch documents') to all
    catch-all subscribers. Best-effort; never raises. Caller commits.

    Catch-all subscribers are those with filter_profile_id IS NULL; passing a
    sentinel profile id that matches no real profile reaches exactly them.
    """
    payload = {"title": title, "body": body, "url": url, "tag": tag or "system"}
    try:
        send_to_subscribers(db, -1, payload)
    except Exception:  # noqa: BLE001
        logger.exception("push.system_notification_failed", title=title)


def send_match_notifications(
    db: Session, tender: Tender, matched_profile_ids: list[int]
) -> None:
    """Convenience: dispatch a "new match" notification for `tender` to subscribers
    of each profile in `matched_profile_ids`. Wrapped in a try/except so a push
    failure can never strand the ingestion transaction.
    """
    if not matched_profile_ids:
        return
    payload = _build_tender_match_payload(tender)
    try:
        for profile_id in matched_profile_ids:
            send_to_subscribers(db, profile_id, payload)
    except Exception:  # noqa: BLE001
        logger.exception("push.dispatch_unexpected_error", tender_id=tender.id)
