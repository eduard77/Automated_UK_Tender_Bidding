"""Microsoft / Outlook provider — Microsoft Graph mail, read-only.

Scopes: ``Mail.Read offline_access User.Read``. Read-only — no Mail.Send. Lists
recent messages via ``$filter=receivedDateTime ge`` and reads bodies +
fileAttachments from Graph.
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

SCOPE = "offline_access User.Read Mail.Read"
GRAPH_BASE = "https://graph.microsoft.com/v1.0/me"
FILE_ATTACHMENT_TYPE = "#microsoft.graph.fileAttachment"


def _authority(tenant: str) -> str:
    return f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0"


class OutlookProvider(EmailProvider):
    name = "outlook"

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        tenant: str = "common",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri
        self._tenant = tenant or "common"
        self._client = client or default_client()

    def configured(self) -> bool:
        return bool(
            self._client_id and self._client_secret and self._redirect_uri
        )

    def _require_configured(self) -> None:
        if not self.configured():
            raise ProviderNotConfiguredError(
                "Outlook is not configured yet — set MS_CLIENT_ID, "
                "MS_CLIENT_SECRET and EMAIL_OAUTH_REDIRECT_URI."
            )

    # --- OAuth ----------------------------------------------------------
    def authorization_url(self, *, state: str) -> str:
        self._require_configured()
        params = {
            "client_id": self._client_id,
            "response_type": "code",
            "redirect_uri": self._redirect_uri,
            "response_mode": "query",
            "scope": SCOPE,
            "state": state,
        }
        return f"{_authority(self._tenant)}/authorize?{urlencode(params)}"

    async def exchange_code(self, code: str) -> OAuthTokens:
        self._require_configured()
        payload = await token_request(
            self._client,
            f"{_authority(self._tenant)}/token",
            {
                "code": code,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "redirect_uri": self._redirect_uri,
                "grant_type": "authorization_code",
                "scope": SCOPE,
            },
        )
        return tokens_from_payload(payload)

    async def refresh(self, tokens: OAuthTokens) -> OAuthTokens:
        self._require_configured()
        if not tokens.refresh_token:
            raise ProviderNotConfiguredError("no refresh token to refresh Outlook")
        payload = await token_request(
            self._client,
            f"{_authority(self._tenant)}/token",
            {
                "refresh_token": tokens.refresh_token,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "grant_type": "refresh_token",
                "scope": SCOPE,
            },
        )
        return tokens_from_payload(payload, fallback_refresh=tokens.refresh_token)

    # --- Mailbox --------------------------------------------------------
    def _auth(self, tokens: OAuthTokens) -> dict[str, str]:
        return {"Authorization": f"Bearer {tokens.access_token}"}

    async def get_address(self, tokens: OAuthTokens) -> str:
        resp = await self._client.get(GRAPH_BASE, headers=self._auth(tokens))
        resp.raise_for_status()
        data = resp.json()
        return data.get("mail") or data.get("userPrincipalName") or ""

    async def list_recent(
        self, tokens: OAuthTokens, *, since: datetime | None, max_results: int
    ) -> list[MessageRef]:
        params: dict[str, str | int] = {
            "$select": "id,receivedDateTime",
            "$orderby": "receivedDateTime desc",
            "$top": max_results,
        }
        if since is not None:
            params["$filter"] = (
                f"receivedDateTime ge {_graph_instant(since)}"
            )
        resp = await self._client.get(
            f"{GRAPH_BASE}/messages",
            headers=self._auth(tokens),
            params=params,
        )
        resp.raise_for_status()
        out: list[MessageRef] = []
        for m in resp.json().get("value", []) or []:
            out.append(
                MessageRef(
                    id=m["id"],
                    received_at=_parse_instant(m.get("receivedDateTime")),
                )
            )
        return out

    async def fetch_message(
        self, tokens: OAuthTokens, message_id: str
    ) -> EmailMessage:
        resp = await self._client.get(
            f"{GRAPH_BASE}/messages/{message_id}",
            headers=self._auth(tokens),
            params={
                "$select": "id,subject,from,receivedDateTime,body,"
                "bodyPreview,hasAttachments"
            },
        )
        resp.raise_for_status()
        msg = resp.json()
        subject = msg.get("subject") or ""
        sender = (
            (msg.get("from") or {}).get("emailAddress", {}).get("address", "")
        )
        received_at = _parse_instant(msg.get("receivedDateTime"))
        body = msg.get("body", {}) or {}
        content = body.get("content") or ""
        if (body.get("contentType") or "").lower() == "html":
            body_text = html_to_text(content)
        else:
            body_text = content.strip()

        attachments: list[EmailAttachment] = []
        if msg.get("hasAttachments"):
            attachments = await self._fetch_attachments(tokens, message_id)

        return EmailMessage(
            id=message_id,
            subject=subject,
            sender=sender,
            received_at=received_at,
            body_text=body_text,
            attachments=attachments,
            links=extract_links(body_text),
        )

    async def _fetch_attachments(
        self, tokens: OAuthTokens, message_id: str
    ) -> list[EmailAttachment]:
        resp = await self._client.get(
            f"{GRAPH_BASE}/messages/{message_id}/attachments",
            headers=self._auth(tokens),
        )
        resp.raise_for_status()
        out: list[EmailAttachment] = []
        for att in resp.json().get("value", []) or []:
            if att.get("@odata.type") != FILE_ATTACHMENT_TYPE:
                # Skip item/reference attachments — we only file real files.
                continue
            content_bytes = att.get("contentBytes")
            if not content_bytes:
                continue
            out.append(
                EmailAttachment(
                    filename=att.get("name") or "attachment",
                    content_type=att.get("contentType"),
                    data=base64.b64decode(content_bytes),
                )
            )
        return out


def _graph_instant(dt: datetime) -> str:
    """Graph $filter wants an ISO instant like 2026-06-09T00:00:00Z."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_instant(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
