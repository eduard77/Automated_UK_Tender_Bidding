"""Shared fixtures + fakes for the email-integration tests.

Everything here is offline: an in-memory SQLite DB (via _billing_fixtures), an
in-memory fake keyring so the token store's Fernet key has somewhere to live,
and fake EmailProvider / LLM implementations so no real mailbox or model is
ever contacted.
"""
from __future__ import annotations

import sys
from datetime import UTC, datetime
from types import ModuleType

from tender_agent.models import Tender
from tender_agent.services.brief.llm_client import LLMResponse
from tender_agent.services.email.providers.base import (
    EmailMessage,
    EmailProvider,
    MessageRef,
    OAuthTokens,
)


def install_fake_keyring(monkeypatch) -> dict:
    """Install an in-memory keyring module (mirrors test_credentials_store)."""
    store: dict[tuple[str, str], str] = {}
    mod = ModuleType("keyring")

    def get_password(service, name):
        return store.get((service, name))

    def set_password(service, name, value):
        store[(service, name)] = value

    def get_keyring():
        return object()

    mod.get_password = get_password  # type: ignore[attr-defined]
    mod.set_password = set_password  # type: ignore[attr-defined]
    mod.get_keyring = get_keyring  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "keyring", mod)
    monkeypatch.setitem(sys.modules, "keyring.backends", None)
    return store


def use_fake_store(monkeypatch, tmp_path) -> None:
    """Point the credentials singleton at a fresh store backed by the fake
    keyring, so encrypt_secret / decrypt_secret (and the token store) work."""
    install_fake_keyring(monkeypatch)
    from tender_agent.services import credentials as creds_mod

    store = creds_mod.CredentialsStore(db_path=str(tmp_path / "creds.db"))
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
