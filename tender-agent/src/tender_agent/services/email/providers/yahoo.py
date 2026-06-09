"""Yahoo provider — DEFERRED.

Yahoo offers no clean read-only mail REST API equivalent to Gmail API or
Microsoft Graph: inbox access is OAuth2 + IMAP (XOAUTH2). Rather than block
Gmail/Outlook on an IMAP read loop, Yahoo occupies the interface slot and
reports "not yet configured" until it is implemented. See the PR description
for the rationale and the IMAP plan.

The slot still satisfies the EmailProvider contract so the rest of the system
treats all three providers uniformly; every method raises ProviderNotConfiguredError
with a clear, user-facing message.
"""
from __future__ import annotations

from datetime import datetime

import httpx

from .base import (
    EmailMessage,
    EmailProvider,
    MessageRef,
    OAuthTokens,
    ProviderNotConfiguredError,
)

_DEFERRED = (
    "Yahoo email is not yet available. Yahoo requires OAuth2 + IMAP rather "
    "than a read-only mail API; it is deferred — connect a Gmail or Outlook "
    "inbox instead. (See the email integration PR for the plan.)"
)


class YahooProvider(EmailProvider):
    name = "yahoo"

    def __init__(
        self,
        *,
        client_id: str = "",
        client_secret: str = "",
        redirect_uri: str = "",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri

    def configured(self) -> bool:
        # Deferred: even with credentials present, the IMAP path isn't built.
        return False

    def authorization_url(self, *, state: str) -> str:
        raise ProviderNotConfiguredError(_DEFERRED)

    async def exchange_code(self, code: str) -> OAuthTokens:
        raise ProviderNotConfiguredError(_DEFERRED)

    async def refresh(self, tokens: OAuthTokens) -> OAuthTokens:
        raise ProviderNotConfiguredError(_DEFERRED)

    async def get_address(self, tokens: OAuthTokens) -> str:
        raise ProviderNotConfiguredError(_DEFERRED)

    async def list_recent(
        self, tokens: OAuthTokens, *, since: datetime | None, max_results: int
    ) -> list[MessageRef]:
        raise ProviderNotConfiguredError(_DEFERRED)

    async def fetch_message(
        self, tokens: OAuthTokens, message_id: str
    ) -> EmailMessage:
        raise ProviderNotConfiguredError(_DEFERRED)
