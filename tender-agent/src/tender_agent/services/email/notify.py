"""Notify the user by push that a tender email was filed + a draft is ready.

Reuses the EXISTING push service (services/push.py) — we do NOT build a new
notifier. The notification names the tender (title + reference), summarises the
email in one line, states how many attachments were filed, and that a suggested
reply draft is ready; tapping it opens the tender where everything lives.

Note: the push subscription system is currently endpoint-scoped (catch-all),
not per-account, so this dispatches to catch-all subscribers via the same path
`send_system_notification` uses. Per-user push is a follow-up (see PR notes).
"""
from __future__ import annotations

import structlog
from sqlalchemy.orm import Session

from tender_agent.config import settings
from tender_agent.models import Tender
from tender_agent.services.push import send_to_subscribers

logger = structlog.get_logger(__name__)

# Sentinel profile id that matches no real filter profile, so the dispatch
# reaches exactly the catch-all subscribers (mirrors send_system_notification).
_CATCH_ALL_SENTINEL = -1


def build_email_match_payload(
    tender: Tender, *, attachment_count: int, draft_ready: bool
) -> dict[str, str]:
    ref = tender.procurement_ref or tender.source_ref or ""
    base = settings.dashboard_base_url.rstrip("/")
    bits = []
    if attachment_count:
        bits.append(
            f"{attachment_count} attachment"
            f"{'s' if attachment_count != 1 else ''} filed"
        )
    bits.append("draft reply ready" if draft_ready else "no reply needed")
    body = f"{tender.title} ({ref}) — " + ", ".join(bits)
    return {
        "title": "Tender email filed",
        "body": body,
        "url": f"{base}/tenders/{tender.id}",
        "tag": f"email-{tender.id}",
    }


def notify_email_match(
    db: Session,
    tender: Tender,
    *,
    attachment_count: int,
    draft_ready: bool,
) -> int:
    """Dispatch exactly one push describing the filed email. Best-effort: never
    raises (a push failure must not strand processing). Returns the number of
    successful sends. Caller commits."""
    payload = build_email_match_payload(
        tender, attachment_count=attachment_count, draft_ready=draft_ready
    )
    try:
        sent, _failed = send_to_subscribers(db, _CATCH_ALL_SENTINEL, payload)
        return sent
    except Exception:  # noqa: BLE001
        logger.exception("email.notify_failed", tender_id=tender.id)
        return 0
