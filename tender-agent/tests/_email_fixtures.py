"""Shared fixtures + fakes for the email-integration tests.

Everything here is offline: an in-memory SQLite DB (via _billing_fixtures), a
credentials store with a throwaway Fernet key so the token store can encrypt,
and fake EmailProvider / LLM implementations so no real mailbox or model is
ever contacted.
"""
from __future__ import annotations

from datetime import UTC, datetime

from tender_agent.models import Tender
from tender_agent.services.brief.llm_client import LLMResponse
from tender_agent.services.email.providers.base import (
    EmailMessage,
    EmailProvider,
    MessageRef,
    OAuthTokens,
)


def use_fake_store(monkeypatch, tmp_path=None) -> None:
    """Point the credentials singleton at a store with a throwaway in-memory
    key, so encrypt_secret / decrypt_secret (and the token store) work with
    no keyring, no env key and no DB. `tmp_path` is unused — kept so existing
    call sites don't change."""
    from cryptography.fernet import Fernet

    from tender_agent.services import credentials as creds_mod

    store = creds_mod.CredentialsStore(
        encryption_key=Fernet.generate_key().decode()
    )
    monkeypatch.setattr(creds_mod, "_store", store)


def make_tender(
    db,
    *,
    source_ref: str = "DN12345",
    procurement_ref: str | None = None,
    source_code: str = "FTS",
    title: str = "Cleaning services",
    buyer_name: str = "Bristol City Council",
) -> Tender:
    now = datetime.now(UTC)
    tender = Tender(
        source_code=source_code,
        source_ref=source_ref,
        procurement_ref=procurement_ref,
        title=title,
        buyer_name=buyer_name,
        first_seen_at=now,
        last_seen_at=now,
    )
    db.add(tender)
    db.commit()
    db.refresh(tender)
    return tender


class FakeStorage:
    """Records puts; never touches disk or Azure."""

    name = "azure_blob"

    def __init__(self):
        self.puts: list[tuple[str, int]] = []

    def put(self, key, data, *, content_type=None):
        self.puts.append((key, len(data)))

    def url(self, key):
        return f"https://acct.blob.core.windows.net/tender-documents/{key}"


class FakeLLM:
    """A BriefLLMClient stand-in returning canned JSON for the draft prompt."""

    model = "fake-claude"

    def __init__(self, text: str | None = None):
        self.text = text or (
            '{"reply_needed": true, "draft": "Thank you — noted, we will '
            'respond before the deadline.", "reasoning": "acknowledgement"}'
        )
        self.calls: list[tuple[str, str, int]] = []

    async def complete(self, *, system: str, user: str, max_tokens: int):
        self.calls.append((system, user, max_tokens))
        return LLMResponse(
            text=self.text, input_tokens=1, output_tokens=1, model=self.model
        )


class FakeProvider(EmailProvider):
    """In-memory EmailProvider. Records which messages were fetched / marked so
    tests can prove links are never fetched and mark_seen is honoured."""

    name = "gmail"

    def __init__(
        self,
        *,
        messages: list[EmailMessage] | None = None,
        address: str = "me@example.com",
        tokens: OAuthTokens | None = None,
    ):
        self._messages = {m.id: m for m in (messages or [])}
        self._address = address
        self._tokens = tokens or OAuthTokens(
            access_token="access", refresh_token="refresh"
        )
        self.fetched: list[str] = []
        self.marked_seen: list[str] = []

    def configured(self) -> bool:
        return True

    def authorization_url(self, *, state: str) -> str:
        return f"https://fake.provider/auth?state={state}"

    async def exchange_code(self, code: str) -> OAuthTokens:
        return self._tokens

    async def refresh(self, tokens: OAuthTokens) -> OAuthTokens:
        return tokens

    async def get_address(self, tokens: OAuthTokens) -> str:
        return self._address

    async def list_recent(self, tokens, *, since, max_results):
        return [MessageRef(id=mid) for mid in self._messages]

    async def fetch_message(self, tokens, message_id: str) -> EmailMessage:
        self.fetched.append(message_id)
        return self._messages[message_id]

    async def mark_seen(self, tokens, message_id: str) -> None:
        self.marked_seen.append(message_id)
