"""End-to-end per-inbox poll, fully offline.

Proves: a matched email files its attachments via the existing ingest path,
stores a draft, and sends exactly ONE push; re-processing the same message is
idempotent (no duplicate files, no second push); links in the body are surfaced
but NEVER fetched (only attachments are pulled).
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from tender_agent.models import (
    Account,
    MailboxAccount,
    MailboxMessage,
    TenderDocumentFile,
)
from tender_agent.services.email import attachments as attachments_mod
from tender_agent.services.email import notify as notify_mod
from tender_agent.services.email.poller import poll_mailbox
from tender_agent.services.email.providers.base import (
    EmailAttachment,
    EmailMessage,
    OAuthTokens,
)
from tender_agent.services.email.token_store import encode_tokens
from tests._billing_fixtures import make_engine_and_session
from tests._email_fixtures import FakeLLM, FakeProvider, FakeStorage, make_tender


@pytest.fixture()
def db():
    _, factory = make_engine_and_session()
    s = factory()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def push_recorder(monkeypatch):
    # Records (target_account_id, payload) so tests prove per-user targeting.
    sent: list[tuple] = []

    def _fake_send(db, account_id, payload, *, filter_profile_id=None):
        sent.append((account_id, payload))
        return (1, 0)

    monkeypatch.setattr(notify_mod, "send_to_account", _fake_send)
    return sent


@pytest.fixture(autouse=True)
def _fake_storage(monkeypatch):
    monkeypatch.setattr(
        attachments_mod, "get_storage_backend", lambda: FakeStorage()
    )


def _account(db) -> Account:
    acc = Account(email="op@example.com", password_hash="x", plan="free")
    db.add(acc)
    db.commit()
    db.refresh(acc)
    return acc


def _mailbox(db, account_id: int) -> MailboxAccount:
    mb = MailboxAccount(
        account_id=account_id,
        provider="gmail",
        email_address="op@gmail.com",
        status="connected",
        token_ciphertext=encode_tokens(
            OAuthTokens(access_token="a", refresh_token="r")
        ),
    )
    db.add(mb)
    db.commit()
    db.refresh(mb)
    return mb


def _message() -> EmailMessage:
    return EmailMessage(
        id="m1",
        subject="RE: DN12345 clarification — please read",
        sender="buyer@council.gov.uk",
        received_at=datetime.now(UTC),
        body_text="Please respond. Docs at https://portal.example/doc1",
        attachments=[
            EmailAttachment("notes.txt", "text/plain", b"clarification text")
        ],
        links=["https://portal.example/doc1"],
    )


@pytest.mark.asyncio
async def test_matched_email_files_drafts_and_notifies_once(
    db, monkeypatch, tmp_path, push_recorder
) -> None:
    from tests._email_fixtures import use_fake_store

    use_fake_store(monkeypatch, tmp_path)
    account = _account(db)
    tender = make_tender(db, source_ref="DN12345")
    mailbox = _mailbox(db, account.id)
    provider = FakeProvider(messages=[_message()])

    summary = await poll_mailbox(db, mailbox, provider=provider, llm=FakeLLM())

    assert summary.filed == 1
    # Attachment filed via the existing ingest path.
    files = db.execute(
        select(TenderDocumentFile).where(
            TenderDocumentFile.tender_id == tender.id
        )
    ).scalars().all()
    assert len(files) == 1

    # A MailboxMessage row with the draft + matched ref + links (not fetched).
    row = db.execute(select(MailboxMessage)).scalar_one()
    assert row.matched_ref == "DN12345"
    assert row.tender_id == tender.id
    assert row.attachment_count == 1
    assert row.draft_status == "drafted"
    assert row.draft_reply
    assert row.links == ["https://portal.example/doc1"]

    # Exactly one push, to the INBOX OWNER, naming tender + ref + attachments.
    assert len(push_recorder) == 1
    target_account_id, payload = push_recorder[0]
    assert target_account_id == account.id
    body = payload["body"]
    assert "Cleaning services" in body
    assert "DN12345" in body
    assert "1 attachment" in body

    # Only the message was fetched; the body link was never requested.
    assert provider.fetched == ["m1"]
    assert provider.marked_seen == ["m1"]


@pytest.mark.asyncio
async def test_reprocessing_same_message_is_idempotent(
    db, monkeypatch, tmp_path, push_recorder
) -> None:
    from tests._email_fixtures import use_fake_store

    use_fake_store(monkeypatch, tmp_path)
    account = _account(db)
    tender = make_tender(db, source_ref="DN12345")
    mailbox = _mailbox(db, account.id)
    provider = FakeProvider(messages=[_message()])

    await poll_mailbox(db, mailbox, provider=provider, llm=FakeLLM())
    second = await poll_mailbox(db, mailbox, provider=provider, llm=FakeLLM())

    assert second.filed == 0
    assert second.skipped_existing == 1
    # No duplicate files, no duplicate message rows, no second push.
    files = db.execute(
        select(TenderDocumentFile).where(
            TenderDocumentFile.tender_id == tender.id
        )
    ).scalars().all()
    assert len(files) == 1
    assert len(db.execute(select(MailboxMessage)).scalars().all()) == 1
    assert len(push_recorder) == 1
    # The duplicate was skipped before fetching it again.
    assert provider.fetched == ["m1"]


@pytest.mark.asyncio
async def test_unmatched_email_is_left_alone(
    db, monkeypatch, tmp_path, push_recorder
) -> None:
    from tests._email_fixtures import use_fake_store

    use_fake_store(monkeypatch, tmp_path)
    account = _account(db)
    make_tender(db, source_ref="DN12345")
    mailbox = _mailbox(db, account.id)
    # Subject carries no known reference.
    msg = _message()
    msg.subject = "Newsletter: this week in procurement"
    provider = FakeProvider(messages=[msg])

    summary = await poll_mailbox(db, mailbox, provider=provider, llm=FakeLLM())

    assert summary.filed == 0
    assert len(db.execute(select(MailboxMessage)).scalars().all()) == 0
    assert len(push_recorder) == 0
