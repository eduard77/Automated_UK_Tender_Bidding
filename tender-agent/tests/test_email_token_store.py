"""OAuth token encryption — reuses the credentials store's Fernet key.

Tokens are encrypted at rest. The roundtrip preserves all fields, including an
absolute (tz-aware) expiry; the ciphertext is opaque (not the plaintext token).
"""
from __future__ import annotations

from datetime import UTC, datetime

from tender_agent.services.email.providers.base import OAuthTokens
from tender_agent.services.email.token_store import decode_tokens, encode_tokens
from tests._email_fixtures import use_fake_store


def test_encode_decode_roundtrip(monkeypatch, tmp_path) -> None:
    use_fake_store(monkeypatch, tmp_path)
    tokens = OAuthTokens(
        access_token="ya29.secret",
        refresh_token="1//refresh",
        expiry=datetime(2026, 6, 9, 12, 0, tzinfo=UTC),
        scope="https://www.googleapis.com/auth/gmail.readonly",
        token_type="Bearer",
    )
    blob = encode_tokens(tokens)
    # Ciphertext is opaque — the raw token must not appear in it.
    assert b"ya29.secret" not in blob
    assert b"1//refresh" not in blob

    out = decode_tokens(blob)
    assert out.access_token == "ya29.secret"
    assert out.refresh_token == "1//refresh"
    assert out.expiry == datetime(2026, 6, 9, 12, 0, tzinfo=UTC)
    assert out.scope.endswith("gmail.readonly")
    assert out.token_type == "Bearer"


def test_roundtrip_without_expiry(monkeypatch, tmp_path) -> None:
    use_fake_store(monkeypatch, tmp_path)
    out = decode_tokens(encode_tokens(OAuthTokens(access_token="a")))
    assert out.access_token == "a"
    assert out.expiry is None
    assert out.refresh_token is None
