"""The EmailProvider abstraction — one interface, three providers.

A provider knows how to: build a consent URL, exchange an auth code for tokens,
refresh tokens, learn the connected address, list recent message ids, and fetch
a full message + attachments. Every provider is READ-ONLY: there is no send
method anywhere in this interface, by design (PROJECT.md §5.8 / CLAUDE.md §7.3).

All network I/O goes through an injected ``httpx.AsyncClient`` so tests drive
the providers fully offline with ``httpx.MockTransport`` — the same convention
the source adapters use (see tests/conftest.py).
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime


class ProviderNotConfiguredError(RuntimeError):
    """The provider's OAuth app credentials aren't set, or the provider is
    deferred (Yahoo). Surfaces to the API as a clean "provider not configured
    yet" message, never a cryptic error."""


@dataclass
class OAuthTokens:
    """Decrypted OAuth tokens for one connected mailbox. SECRET — never logged.

    ``expiry`` is an absolute UTC instant (not a relative ``expires_in``) so the
    poller can decide whether to refresh without tracking when it was issued.
    """

    access_token: str
    refresh_token: str | None = None
    expiry: datetime | None = None
    scope: str | None = None
    token_type: str = "Bearer"


@dataclass
class EmailAttachment:
    filename: str
    content_type: str | None
    data: bytes


@dataclass
class EmailMessage:
    """A fetched message. ``links`` are plain-text URLs found in the body and
    surfaced to the user — the system NEVER fetches them (attachments only)."""

    id: str
    subject: str
    sender: str
    received_at: datetime | None
    body_text: str
    attachments: list[EmailAttachment] = field(default_factory=list)
    links: list[str] = field(default_factory=list)


@dataclass
class MessageRef:
    """A lightweight listing entry — id plus best-effort received time."""

    id: str
    received_at: datetime | None = None


class EmailProvider(ABC):
    """Common shape for Gmail, Outlook and Yahoo.

    Concrete providers take their OAuth app config + an httpx client in
    ``__init__``. ``configured()`` reports whether the one-time operator setup
    (client id/secret/redirect) has been done.
    """

    name: str

    # --- OAuth ----------------------------------------------------------
    @abstractmethod
    def configured(self) -> bool:
        """True iff this provider's OAuth app credentials are set."""

    @abstractmethod
    def authorization_url(self, *, state: str) -> str:
        """Build the provider consent URL the user is redirected to."""

    @abstractmethod
    async def exchange_code(self, code: str) -> OAuthTokens:
        """Exchange an authorization code for tokens (initial connect)."""

    @abstractmethod
    async def refresh(self, tokens: OAuthTokens) -> OAuthTokens:
        """Refresh an expired access token using the refresh token."""

    # --- Mailbox --------------------------------------------------------
    @abstractmethod
    async def get_address(self, tokens: OAuthTokens) -> str:
        """Return the connected mailbox's email address."""

    @abstractmethod
    async def list_recent(
        self, tokens: OAuthTokens, *, since: datetime | None, max_results: int
    ) -> list[MessageRef]:
        """List recent message ids (newest first), optionally since an instant."""

    @abstractmethod
    async def fetch_message(
        self, tokens: OAuthTokens, message_id: str
    ) -> EmailMessage:
        """Fetch a full message with body text + all attachments."""

    async def mark_seen(self, tokens: OAuthTokens, message_id: str) -> None:
        """No-op by default.

        Read-only OAuth scope cannot mark messages on the server, and we do not
        want to: idempotency is enforced locally via the mailbox_messages table
        (unique provider message id), not by mutating the user's inbox.
        """
        return


_URL_RE = re.compile(r"https?://[^\s<>\"')]+", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")


def extract_links(text: str) -> list[str]:
    """Extract http(s) links from body text, de-duplicated, order preserved.

    These are surfaced to the user as plain text only — never fetched.
    """
    seen: set[str] = set()
    out: list[str] = []
    for m in _URL_RE.findall(text or ""):
        url = m.rstrip(".,);]}>\"'")
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def html_to_text(html: str) -> str:
    """Very small HTML→text reduction for draft grounding. Not a full parser —
    strips tags and collapses whitespace, which is enough to give the LLM the
    readable content of an HTML email body."""
    if not html:
        return ""
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p>", "\n", text)
    text = _TAG_RE.sub(" ", text)
    # Unescape the handful of entities that actually matter for readability.
    for ent, ch in (
        ("&amp;", "&"),
        ("&lt;", "<"),
        ("&gt;", ">"),
        ("&quot;", '"'),
        ("&#39;", "'"),
        ("&nbsp;", " "),
    ):
        text = text.replace(ent, ch)
    lines = [_WS_RE.sub(" ", ln).strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln).strip()
