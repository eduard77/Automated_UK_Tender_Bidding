"""Per-inbox poll: list -> match -> file -> draft -> notify -> mark-seen.

Runs on a schedule, never on a user-request path. Idempotent: a message is
processed at most once per mailbox, guarded by the unique
(mailbox_account_id, provider_message_id) row — re-processing files no
duplicate attachments and sends no duplicate notification.

The system SUGGESTS only: it files the email, drafts a reply for review, and
notifies. It never sends anything. Links in the body are surfaced to the user
as plain text but NEVER fetched (attachments only).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tender_agent.config import settings
from tender_agent.db import SessionLocal
from tender_agent.models import MailboxAccount, MailboxMessage, Tender
from tender_agent.services.email.attachments import file_email_attachments
from tender_agent.services.email.draft import generate_draft
from tender_agent.services.email.matching import match_subject_to_tender
from tender_agent.services.email.notify import notify_email_match
from tender_agent.services.email.providers import (
    EmailProvider,
    build_provider,
    fresh_tokens,
)
from tender_agent.services.email.providers.base import EmailMessage, OAuthTokens
from tender_agent.services.email.token_store import decode_tokens, encode_tokens

logger = structlog.get_logger(__name__)

_BODY_EXCERPT_CHARS = 600


@dataclass
class MessageOutcome:
    message_id: str
    # 'filed' | 'skipped_existing' | 'no_match' | 'ref_no_tender' | 'error'
    status: str
    tender_id: int | None = None
    attachment_count: int = 0
    draft_status: str | None = None
    notified: int = 0


@dataclass
class PollSummary:
    mailbox_id: int
    listed: int = 0
    filed: int = 0
    skipped_existing: int = 0
    no_match: int = 0
    errors: int = 0
    outcomes: list[MessageOutcome] = field(default_factory=list)


def _since_for(mailbox: MailboxAccount) -> datetime:
    """Incremental window: from the last poll minus an overlap, or an initial
    lookback on the very first poll. The overlap is safe because of idempotency.
    """
    if mailbox.last_polled_at is not None:
        base = mailbox.last_polled_at
        if base.tzinfo is None:
            base = base.replace(tzinfo=UTC)
        return base - timedelta(minutes=settings.email_poll_overlap_minutes)
    return datetime.now(UTC) - timedelta(
        days=settings.email_initial_lookback_days
    )


def _already_processed(
    db: Session, mailbox_id: int, message_id: str
) -> bool:
    return (
        db.execute(
            select(MailboxMessage.id)
            .where(MailboxMessage.mailbox_account_id == mailbox_id)
            .where(MailboxMessage.provider_message_id == message_id)
            .limit(1)
        ).scalar_one_or_none()
        is not None
    )


async def _ensure_tokens(
    db: Session, mailbox: MailboxAccount, provider: EmailProvider
) -> OAuthTokens:
    """Decrypt the mailbox tokens and refresh-in-place if near expiry,
    persisting the new ciphertext so a refresh is never lost."""
    tokens = decode_tokens(mailbox.token_ciphertext)
    tokens, changed = await fresh_tokens(provider, tokens)
    if changed:
        mailbox.token_ciphertext = encode_tokens(tokens)
        mailbox.updated_at = datetime.now(UTC)
        db.commit()
    return tokens


async def _process_message(
    db: Session,
    mailbox: MailboxAccount,
    message: EmailMessage,
    *,
    llm: object | None,
) -> MessageOutcome:
    """Match -> file -> draft -> persist -> notify for one fetched message."""
    match = match_subject_to_tender(db, message.subject)
    if match.tender_id is None:
        if match.unmatched_ref_shaped:
            # A reference-shaped token we hold no tender for: log the miss so
            # it's visible, but take no further action (by design).
            logger.info(
                "email.ref_no_tender",
                mailbox_id=mailbox.id,
                refs=match.unmatched_ref_shaped,
            )
            return MessageOutcome(message.id, "ref_no_tender")
        return MessageOutcome(message.id, "no_match")

    tender = db.get(Tender, match.tender_id)
    if tender is None:  # pragma: no cover - referential safety
        return MessageOutcome(message.id, "no_match")

    # File attachments via the existing ingest path (commits internally).
    ingest = file_email_attachments(db, tender.id, message.attachments)
    attachment_count = ingest.inserted + ingest.deduped

    # Draft a SUGGESTED reply (never sent).
    draft = await generate_draft(message, tender, llm=llm)  # type: ignore[arg-type]

    row = MailboxMessage(
        mailbox_account_id=mailbox.id,
        provider_message_id=message.id,
        tender_id=tender.id,
        matched_ref=match.matched_ref,
        subject=message.subject,
        sender=message.sender,
        received_at=message.received_at,
        body_excerpt=(message.body_text or "")[:_BODY_EXCERPT_CHARS] or None,
        attachment_count=attachment_count,
        links=message.links or None,
        draft_reply=draft.draft_text or None,
        draft_status=draft.status,
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        # Race: another poll claimed this message id first. Not an error — and
        # crucially we must NOT notify again.
        db.rollback()
        return MessageOutcome(message.id, "skipped_existing", tender_id=tender.id)

    notified = notify_email_match(
        db,
        tender,
        attachment_count=attachment_count,
        draft_ready=(draft.status == "drafted"),
    )
    db.commit()

    logger.info(
        "email.message_filed",
        mailbox_id=mailbox.id,
        tender_id=tender.id,
        matched_ref=match.matched_ref,
        attachments=attachment_count,
        draft_status=draft.status,
        notified=notified,
    )
    return MessageOutcome(
        message.id,
        "filed",
        tender_id=tender.id,
        attachment_count=attachment_count,
        draft_status=draft.status,
        notified=notified,
    )


async def poll_mailbox(
    db: Session,
    mailbox: MailboxAccount,
    *,
    provider: EmailProvider | None = None,
    llm: object | None = None,
) -> PollSummary:
    """Run one poll cycle for a connected mailbox. `provider`/`llm` are
    injectable for tests; production builds them from config."""
    summary = PollSummary(mailbox_id=mailbox.id)
    if mailbox.status != "connected" or mailbox.token_ciphertext is None:
        return summary

    provider = provider or build_provider(mailbox.provider)
    started_at = datetime.now(UTC)
    tokens = await _ensure_tokens(db, mailbox, provider)
    since = _since_for(mailbox)

    refs = await provider.list_recent(
        tokens, since=since, max_results=settings.email_poll_max_messages
    )
    summary.listed = len(refs)

    for ref in refs:
        if _already_processed(db, mailbox.id, ref.id):
            summary.skipped_existing += 1
            summary.outcomes.append(MessageOutcome(ref.id, "skipped_existing"))
            continue
        try:
            message = await provider.fetch_message(tokens, ref.id)
            outcome = await _process_message(db, mailbox, message, llm=llm)
            # Read-only: a no-op for our providers, but honour the interface.
            await provider.mark_seen(tokens, ref.id)
        except Exception as exc:  # noqa: BLE001 — one bad message can't stop the poll
            db.rollback()
            logger.warning(
                "email.message_failed",
                mailbox_id=mailbox.id,
                message_id=ref.id,
                error=str(exc),
            )
            summary.errors += 1
            summary.outcomes.append(MessageOutcome(ref.id, "error"))
            continue

        summary.outcomes.append(outcome)
        if outcome.status == "filed":
            summary.filed += 1
        elif outcome.status == "skipped_existing":
            summary.skipped_existing += 1
        else:
            summary.no_match += 1

    # Advance the watermark only after a clean listing pass.
    mailbox.last_polled_at = started_at
    mailbox.last_error = None
    mailbox.updated_at = datetime.now(UTC)
    db.commit()
    logger.info(
        "email.poll_complete",
        mailbox_id=mailbox.id,
        listed=summary.listed,
        filed=summary.filed,
        skipped=summary.skipped_existing,
        errors=summary.errors,
    )
    return summary


async def poll_all_mailboxes() -> None:
    """Scheduled entry point: poll every connected mailbox. Per-mailbox failures
    are isolated and logged; one inbox never blocks another."""
    if not settings.email_poll_enabled:
        return
    with SessionLocal() as db:
        mailboxes = list(
            db.execute(
                select(MailboxAccount).where(
                    MailboxAccount.status == "connected"
                )
            )
            .scalars()
            .all()
        )
        for mailbox in mailboxes:
            try:
                await poll_mailbox(db, mailbox)
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                logger.exception(
                    "email.mailbox_poll_failed", mailbox_id=mailbox.id
                )
                mailbox.last_error = str(exc)[:500]
                mailbox.status = "error"
                db.commit()
