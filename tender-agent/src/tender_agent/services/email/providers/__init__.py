"""Provider registry: name -> EmailProvider, built from config.

One general interface (`EmailProvider`), three concrete providers. Gmail and
Outlook are fully implemented; Yahoo is a deferred slot that reports
"not yet configured". `build_provider` reads the per-provider OAuth app
credentials from settings, so a missing one-time operator setup surfaces as a
clean ProviderNotConfiguredError rather than a cryptic error.
"""
from __future__ import annotations

import httpx

from tender_agent.config import settings

from ._oauth import is_expired
from .base import EmailProvider, OAuthTokens, ProviderNotConfiguredError
from .gmail import GmailProvider
from .outlook import OutlookProvider
from .yahoo import YahooProvider

# Canonical provider names exposed by the connect API. "microsoft" is accepted
# as an alias for "outlook".
PROVIDER_NAMES = ("gmail", "outlook", "yahoo")
_ALIASES = {"microsoft": "outlook", "google": "gmail"}


def canonical_provider(name: str) -> str:
    name = (name or "").strip().lower()
    return _ALIASES.get(name, name)


def build_provider(
    name: str, *, client: httpx.AsyncClient | None = None
) -> EmailProvider:
    """Construct a provider by name. Raises ProviderNotConfiguredError for an
    unknown name (the credential check is separate — see `configured()`)."""
    name = canonical_provider(name)
    if name == "gmail":
        return GmailProvider(
            client_id=settings.gmail_client_id,
            client_secret=settings.gmail_client_secret,
            redirect_uri=settings.email_oauth_redirect_uri,
            client=client,
        )
    if name == "outlook":
        return OutlookProvider(
            client_id=settings.ms_client_id,
            client_secret=settings.ms_client_secret,
            redirect_uri=settings.email_oauth_redirect_uri,
            tenant=settings.ms_tenant,
            client=client,
        )
    if name == "yahoo":
        return YahooProvider(
            client_id=settings.yahoo_client_id,
            client_secret=settings.yahoo_client_secret,
            redirect_uri=settings.email_oauth_redirect_uri,
            client=client,
        )
    raise ProviderNotConfiguredError(f"unknown email provider: {name!r}")


def provider_configured(name: str) -> bool:
    """True iff the named provider's one-time operator setup is complete."""
    try:
        return build_provider(name).configured()
    except ProviderNotConfiguredError:
        return False


async def fresh_tokens(
    provider: EmailProvider, tokens: OAuthTokens
) -> tuple[OAuthTokens, bool]:
    """Return tokens guaranteed fresh enough to use, plus whether they changed.

    Refreshes via the provider when the access token is at/near expiry. The
    caller persists the new tokens when `changed` is True.
    """
    if is_expired(tokens) and tokens.refresh_token:
        refreshed = await provider.refresh(tokens)
        return refreshed, True
    return tokens, False


__all__ = [
    "EmailProvider",
    "OAuthTokens",
    "ProviderNotConfiguredError",
    "PROVIDER_NAMES",
    "build_provider",
    "canonical_provider",
    "fresh_tokens",
    "provider_configured",
]
