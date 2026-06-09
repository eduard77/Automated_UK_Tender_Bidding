"""Gmail provider — Gmail API over httpx, OAuth scope gmail.readonly.

Read-only: the scope grants no send/modify capability. Listing uses Gmail's
``q=after:<epoch>`` search to fetch only recent mail; messages are read with
``format=full`` and attachments pulled from the attachments endpoint.
"""
from __future__ import annotations

import base64
from datetime import UTC, datetime
from urllib.parse import urlencode

import httpx
import structlog

from ._oauth import default_client, token_request, tokens_from_payload
from .base import (
    EmailAttachment,
    EmailMessage,
    EmailProvider,
    MessageRef,
    OAuthTokens,
    ProviderNotConfiguredError,
    extract_links,
    html_to_text,
)

logger = structlog.get_logger(__name__)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
SCOPE = "https://www.googleapis.com/auth/gmail.readonly"


def _b64url_decode(data: str) -> bytes:
    if not data:
        return b""
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


class GmailProvider(EmailProvider):
    name = "gmail"

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri
        self._client = client or default_client()

    def configured(self) -> bool:
        return bool(
            self._client_id and self._client_secret and self._redirect_uri
        )

    def _require_configured(self) -> None:
        if not self.configured():
            raise ProviderNotConfiguredError(
                "Gmail is not configured yet — set GMAIL_CLIENT_ID, "
                "GMAIL_CLIENT_SECRET and EMAIL_OAUTH_REDIRECT_URI."
            )

    # --- OAuth ----------------------------------------------------------
    def authorization_url(self, *, state: str) -> str:
        self._require_configured()
        params = {
            "client_id": self._client_id,
            "redirect_uri": self._redirect_uri,
            "response_type": "code",
            "scope": SCOPE,
            "access_type": "offline",
            # Force a refresh token even on re-consent.
            "prompt": "consent",
            "include_granted_scopes": "true",
            "state": state,
        }
        return f"{AUTH_URL}?{urlencode(params)}"

    async def exchange_code(self, code: str) -> OAuthTokens:
        self._require_configured()
        payload = await token_request(
            self._client,
            TOKEN_URL,
            {
                "code": code,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "redirect_uri": self._redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        return tokens_from_payload(payload)

    async def refresh(self, tokens: OAuthTokens) -> OAuthTokens:
        self._require_configured()
        if not tokens.refresh_token:
            raise ProviderNotConfiguredError("no refresh token to refresh Gmail")
        payload = await token_request(
            self._client,
            TOKEN_URL,
            {
                "refresh_token": tokens.refresh_token,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "grant_type": "refresh_token",
            },
        )
        # Google omits refresh_token on refresh — keep the existing one.
        return tokens_from_payload(payload, fallback_refresh=tokens.refresh_token)

    # --- Mailbox --------------------------------------------------------
    def _auth(self, tokens: OAuthTokens) -> dict[str, str]:
        return {"Authorization": f"Bearer {tokens.access_token}"}

    async def get_address(self, tokens: OAuthTokens) -> str:
        resp = await self._client.get(
            f"{API_BASE}/profile", headers=self._auth(tokens)
        )
        resp.raise_for_status()
        return resp.json().get("emailAddress", "")

    async def list_recent(
        self, tokens: OAuthTokens, *, since: datetime | None, max_results: int
    ) -> list[MessageRef]:
        params: dict[str, str | int] = {"maxResults": max_results}
        if since is not None:
            params["q"] = f"after:{int(since.timestamp())}"
        resp = await self._client.get(
            f"{API_BASE}/messages",
            headers=self._auth(tokens),
            params=params,
        )
        resp.raise_for_status()
        data = resp.json()
        return [MessageRef(id=m["id"]) for m in data.get("messages", []) or []]

    async def fetch_message(
        self, tokens: OAuthTokens, message_id: str
    ) -> EmailMessage:
        resp = await self._client.get(
            f"{API_BASE}/messages/{message_id}",
            headers=self._auth(tokens),
            params={"format": "full"},
        )
        resp.raise_for_status()
        msg = resp.json()
        payload = msg.get("payload", {}) or {}
        headers = {
            h.get("name", "").lower(): h.get("value", "")
            for h in payload.get("headers", []) or []
        }
        subject = headers.get("subject", "")
        sender = headers.get("from", "")
        received_at = _internal_date(msg.get("internalDate"))

        text_parts: list[str] = []
        html_parts: list[str] = []
        attachment_specs: list[tuple[str, str | None, str]] = []
        _walk_parts(payload, text_parts, html_parts, attachment_specs)

        body_text = "\n".join(p for p in text_parts if p).strip()
        if not body_text and html_parts:
            body_text = html_to_text("\n".join(html_parts))

        attachments: list[EmailAttachment] = []
        for filename, content_type, attachment_id in attachment_specs:
            data = await self._fetch_attachment(
                tokens, message_id, attachment_id
            )
            attachments.append(
                EmailAttachment(
                    filename=filename, content_type=content_type, data=data
                )
            )

        return EmailMessage(
            id=message_id,
            subject=subject,
            sender=sender,
            received_at=received_at,
            body_text=body_text,
            attachments=attachments,
            links=extract_links(body_text),
        )

    async def _fetch_attachment(
        self, tokens: OAuthTokens, message_id: str, attachment_id: str
    ) -> bytes:
        resp = await self._client.get(
            f"{API_BASE}/messages/{message_id}/attachments/{attachment_id}",
            headers=self._auth(tokens),
        )
        resp.raise_for_status()
        return _b64url_decode(resp.json().get("data", ""))


def _internal_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=UTC)
    except (ValueError, TypeError):
        return None


def _walk_parts(
    part: dict,
    text_parts: list[str],
    html_parts: list[str],
    attachments: list[tuple[str, str | None, str]],
) -> None:
    """Recursively collect text/html bodies and attachment specs from a Gmail
    MIME payload tree."""
    mime = (part.get("mimeType") or "").lower()
    filename = part.get("filename") or ""
    body = part.get("body", {}) or {}
    attachment_id = body.get("attachmentId")

    if filename and attachment_id:
        attachments.append((filename, part.get("mimeType"), attachment_id))
    elif mime == "text/plain":
        text_parts.append(_b64url_decode(body.get("data", "")).decode(
            "utf-8", errors="replace"
        ))
    elif mime == "text/html":
        html_parts.append(_b64url_decode(body.get("data", "")).decode(
            "utf-8", errors="replace"
        ))

    for sub in part.get("parts", []) or []:
        _walk_parts(sub, text_parts, html_parts, attachments)
