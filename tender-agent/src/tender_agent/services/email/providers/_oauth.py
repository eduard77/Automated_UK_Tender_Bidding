"""Shared OAuth2 helpers for the REST-based providers (Gmail, Outlook).

Both Google and Microsoft speak standard OAuth2 authorization-code flow with a
form-encoded token endpoint that returns ``access_token`` / ``refresh_token`` /
``expires_in``. These helpers keep that one shape in one place; provider files
only hold their own endpoint URLs + scopes.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import structlog

from tender_agent.config import settings

from .base import OAuthTokens, ProviderNotConfiguredError

logger = structlog.get_logger(__name__)

# Refresh a little before the real expiry so an in-flight poll never races the
# token going stale.
EXPIRY_MARGIN_SECONDS = 120


def default_client() -> httpx.AsyncClient:
    """A plain async client matching the source-adapter convention. Tests pass
    a MockTransport-backed client instead."""
    return httpx.AsyncClient(
        timeout=settings.http_timeout_seconds,
        headers={"User-Agent": settings.http_user_agent},
    )


def tokens_from_payload(
    payload: dict, *, fallback_refresh: str | None = None
) -> OAuthTokens:
    """Build OAuthTokens from a token-endpoint JSON response, converting the
    relative ``expires_in`` to an absolute UTC ``expiry``. Providers that omit a
    refresh token on refresh (Google) keep the prior one via fallback."""
    expires_in = payload.get("expires_in")
    expiry: datetime | None = None
    if isinstance(expires_in, (int, float)):
        expiry = datetime.now(UTC) + timedelta(seconds=int(expires_in))
    return OAuthTokens(
        access_token=payload["access_token"],
        refresh_token=payload.get("refresh_token") or fallback_refresh,
        expiry=expiry,
        scope=payload.get("scope"),
        token_type=payload.get("token_type") or "Bearer",
    )


def is_expired(tokens: OAuthTokens) -> bool:
    """True if the access token is missing an expiry or is within the refresh
    margin of expiring."""
    if tokens.expiry is None:
        return False
    return datetime.now(UTC) >= tokens.expiry - timedelta(
        seconds=EXPIRY_MARGIN_SECONDS
    )


async def token_request(
    client: httpx.AsyncClient, token_url: str, data: dict[str, str]
) -> dict:
    """POST a form-encoded token request and return the JSON. Raises a clean
    error on failure WITHOUT leaking secret values into logs."""
    resp = await client.post(
        token_url,
        data=data,
        headers={"Accept": "application/json"},
    )
    if resp.status_code >= 400:
        # Log status + provider error code only — never the request body
        # (which carries the client secret / auth code).
        detail = ""
        try:
            detail = str(resp.json().get("error", ""))
        except Exception:  # noqa: BLE001
            detail = ""
        logger.warning(
            "email.token_request_failed", status=resp.status_code, error=detail
        )
        raise ProviderNotConfiguredError(
            f"OAuth token request failed (status {resp.status_code})"
        )
    return resp.json()
