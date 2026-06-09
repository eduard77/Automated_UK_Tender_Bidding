"""Encrypt/decrypt OAuth tokens at rest.

Tokens are SECRETS. We reuse the portal credentials store's keyring-held Fernet
key (services/credentials.py) rather than introduce a second secret mechanism —
"reuse that mechanism" per the spec. The ciphertext lives on the per-account
mailbox_accounts row (Postgres), so this module only does the
serialise + encrypt / decrypt + deserialise; it never logs token values.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime

from tender_agent.services.credentials import get_store
from tender_agent.services.email.providers.base import OAuthTokens


def encode_tokens(tokens: OAuthTokens) -> bytes:
    """Serialise + Fernet-encrypt tokens to a ciphertext blob for storage."""
    payload = {
        "access_token": tokens.access_token,
        "refresh_token": tokens.refresh_token,
        "expiry": tokens.expiry.isoformat() if tokens.expiry else None,
        "scope": tokens.scope,
        "token_type": tokens.token_type,
    }
    return get_store().encrypt_secret(json.dumps(payload))


def decode_tokens(blob: bytes) -> OAuthTokens:
    """Decrypt + deserialise a blob produced by `encode_tokens`."""
    data = json.loads(get_store().decrypt_secret(blob))
    expiry_raw = data.get("expiry")
    expiry = None
    if expiry_raw:
        expiry = datetime.fromisoformat(expiry_raw)
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=UTC)
    return OAuthTokens(
        access_token=data["access_token"],
        refresh_token=data.get("refresh_token"),
        expiry=expiry,
        scope=data.get("scope"),
        token_type=data.get("token_type") or "Bearer",
    )
