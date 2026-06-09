"""Web Push dispatch — scoped per-user.

Every notification targets a SPECIFIC account's subscriptions. There is no
catch-all path: a notification is never sent to subscriptions across accounts,
and a missing/unknown target account notifies NOBODY (logged), never everybody.
This is what stops one user's notifications — especially the email-derived ones
that carry private inbox content — reaching another user's devices.

Legacy `account_id IS NULL` subscriptions are unowned and are excluded from
every dispatch (they are never blasted); they stop receiving until the device
re-subscribes (now authenticated) or an operator backfills them.

Best-effort: a failure here MUST NOT break ingestion, email processing, or any
caller — we always log structured events and swallow exceptions at the dispatch
boundary. Subscriptions whose endpoint returns 404/410 ("gone") are deleted;
successful sends update last_used_at.
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


def _dispatch(
    db: Session, subs: list[PushSubscription], payload: dict[str, str]
) -> tuple[int, int]:
    """Send `payload` to a pre-selected list of subscriptions. Returns
    (sent, failed). Does NOT raise. Caller commits."""
    sent = failed = 0
    for sub in subs:
        if _send_one(db, sub, payload):
            sent += 1
        else:
            failed += 1
    logger.info(
        "push.dispatch_complete", sent=sent, failed=failed, total=len(subs)
    )
    return (sent, failed)


def send_to_account(
    db: Session,
    account_id: int | None,
    payload: dict[str, str],
    *,
    filter_profile_id: int | None = None,
) -> tuple[int, int]:
    """Dispatch `payload` to ONE account's subscriptions. Returns (sent, failed).
    Does NOT raise. Caller commits.

    Safe failure: a None/unknown `account_id` notifies NOBODY (logged), never
    everybody. When `filter_profile_id` is given, only this account's
    subscriptions pinned to that profile (or its "all matches" catch-all subs)
    are targeted; otherwise ALL of the account's devices are targeted (used for
    account-specific events like a filed email).
    """
    if not push_configured():
        logger.info("push.skip_unconfigured", account_id=account_id)
        return (0, 0)
    if account_id is None:
        # The whole point of this module: no target => no recipients.
        logger.warning("push.no_target_account")
        return (0, 0)

    stmt = select(PushSubscription).where(
        PushSubscription.account_id == account_id
    )
    if filter_profile_id is not None:
        stmt = stmt.where(
            or_(
                PushSubscription.filter_profile_id == filter_profile_id,
                PushSubscription.filter_profile_id.is_(None),
            )
        )
    subs = list(db.execute(stmt).scalars().all())
    if not subs:
        logger.info("push.no_subscribers", account_id=account_id)
        return (0, 0)
    return _dispatch(db, subs, payload)


def send_system_notification(
    db: Session, title: str, body: str, url: str, tag: str | None = None
) -> None:
    """Dispatch an operational/system alert (e.g. "log in to fetch documents").

    These aren't tied to an end user, so they go to the configured OPERATOR
    account's devices only (settings.push_operator_account_id). If that is unset,
    they notify nobody — never a cross-account broadcast. Best-effort; never
    raises. Caller commits.
    """
    payload = {"title": title, "body": body, "url": url, "tag": tag or "system"}
    try:
        send_to_account(db, settings.push_operator_account_id, payload)
    except Exception:  # noqa: BLE001
        logger.exception("push.system_notification_failed", title=title)


def send_match_notifications(
    db: Session, tender: Tender, matched_profile_ids: list[int]
) -> None:
    """Dispatch a "new match" notification for `tender`, per-user.

    Targets only OWNED subscriptions that are pinned to one of `matched_profile_ids`
    (plus owned "all matches" catch-all subscriptions). Legacy NULL-owner rows are
    excluded. Each subscription is delivered to its own account — there is no
    cross-account dispatch. Wrapped so a push failure can never strand the
    ingestion transaction.
    """
    if not matched_profile_ids:
        return
    if not push_configured():
        logger.info("push.skip_unconfigured", tender_id=tender.id)
        return
    payload = _build_tender_match_payload(tender)
    try:
        subs = list(
            db.execute(
                select(PushSubscription).where(
                    PushSubscription.account_id.isnot(None),
                    or_(
                        PushSubscription.filter_profile_id.in_(
                            matched_profile_ids
                        ),
                        PushSubscription.filter_profile_id.is_(None),
                    ),
                )
            )
            .scalars()
            .all()
        )
        if not subs:
            logger.info("push.no_subscribers", tender_id=tender.id)
            return
        _dispatch(db, subs, payload)
    except Exception:  # noqa: BLE001
        logger.exception("push.dispatch_unexpected_error", tender_id=tender.id)
