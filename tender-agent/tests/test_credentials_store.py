"""CredentialsStore — cloud-safe backend (encrypted column in the app DB).

All offline: an in-memory SQLite engine stands in for Postgres, cryptography
(Fernet) is real. Covers the key-resolution order (explicit / env setting /
keyring fallback), the encryption roundtrip + lifecycle, and the fail-safe
paths: no key -> instructive error naming the app setting; bad key format ->
instructive error; wrong/rotated key -> clean error, no plaintext anywhere.
"""
from __future__ import annotations

import json
import sys
from types import ModuleType

import pytest
from cryptography.fernet import Fernet
from structlog.testing import capture_logs

from tender_agent.config import settings
from tender_agent.models import PortalCredential
from tender_agent.services.credentials import (
    CredentialsStore,
    CredentialsStoreError,
)
from tender_agent.services.portals.base import Credentials
from tests._billing_fixtures import make_engine_and_session


def _make_store(key: str | None = None):
    """A store bound to a fresh in-memory DB. Returns (store, session_factory)
    so tests can inspect the raw rows / build a second store on the same DB."""
    _engine, factory = make_engine_and_session()
    if key is None:
        key = Fernet.generate_key().decode()
    return CredentialsStore(session_factory=factory, encryption_key=key), factory


def _install_fake_keyring(monkeypatch, *, working: bool = True) -> dict:
    store: dict[tuple[str, str], str] = {}
    mod = ModuleType("keyring")

    if working:
        def get_password(service, name):
            return store.get((service, name))

        def set_password(service, name, value):
            store[(service, name)] = value

        def get_keyring():
            return object()  # any non-Fail backend
    else:
        def get_password(service, name):
            raise RuntimeError("no keyring backend")

        def set_password(service, name, value):
            raise RuntimeError("no keyring backend")

        def get_keyring():
            return object()

    mod.get_password = get_password  # type: ignore[attr-defined]
    mod.set_password = set_password  # type: ignore[attr-defined]
    mod.get_keyring = get_keyring  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "keyring", mod)
    # Ensure the keyring.backends.fail probe import fails cleanly.
    monkeypatch.setitem(sys.modules, "keyring.backends", None)
    return store


# --- roundtrip + lifecycle (parity with the legacy store) -------------------


def test_store_and_get_roundtrip() -> None:
    store, _ = _make_store()
    creds = Credentials(
        username="alice",
        password="s3cret",
        email="alice@example.com",
        extra={"buyer_ref": "ABC"},
    )
    store.store_credentials(10, "eduard", creds, platform_slug="delta_esourcing")
    got = store.get_credentials(10, "eduard")
    assert got is not None
    assert got.username == "alice"
    assert got.password == "s3cret"
    assert got.email == "alice@example.com"
    assert got.extra == {"buyer_ref": "ABC"}


def test_password_not_stored_in_plaintext() -> None:
    store, factory = _make_store()
    store.store_credentials(
        11, "eduard", Credentials(username="bob", password="HUNTER2PLAIN")
    )
    with factory() as db:
        row = db.get(PortalCredential, (11, "eduard"))
        assert row is not None
        assert b"HUNTER2PLAIN" not in row.secret_ciphertext
        assert b"bob" not in row.secret_ciphertext


def test_list_returns_metadata_only() -> None:
    store, _ = _make_store()
    store.store_credentials(
        1, "eduard", Credentials(username="a", password="p"), platform_slug="in_tend"
    )
    store.store_credentials(
        2, "eduard", Credentials(username="b", password="q"), platform_slug="jaggaer"
    )
    metas = store.list_credentials("eduard")
    assert {m.portal_id for m in metas} == {1, 2}
    slugs = {m.platform_slug for m in metas}
    assert slugs == {"in_tend", "jaggaer"}
    assert all(m.valid for m in metas)
    # Metadata carries no secret-shaped fields at all.
    assert not any(hasattr(m, "password") for m in metas)


def test_upsert_overwrites_and_revalidates() -> None:
    store, _ = _make_store()
    store.store_credentials(3, "eduard", Credentials(username="a", password="old"))
    store.mark_invalid(3, "eduard")
    store.store_credentials(3, "eduard", Credentials(username="a", password="new"))
    got = store.get_credentials(3, "eduard")
    assert got is not None and got.password == "new"
    assert store.list_credentials("eduard")[0].valid is True


def test_mark_invalid_and_validated() -> None:
    store, _ = _make_store()
    store.store_credentials(5, "eduard", Credentials(username="x", password="y"))
    store.mark_invalid(5, "eduard")
    assert store.list_credentials("eduard")[0].valid is False
    store.mark_validated(5, "eduard")
    meta = store.list_credentials("eduard")[0]
    assert meta.valid is True
    assert meta.last_validated_at is not None


def test_soft_delete_hides_from_list_and_get() -> None:
    store, _ = _make_store()
    store.store_credentials(9, "eduard", Credentials(username="x", password="y"))
    store.delete_credentials(9, "eduard")
    assert store.list_credentials("eduard") == []
    assert store.get_credentials(9, "eduard") is None


# --- key resolution ----------------------------------------------------------


def test_key_from_settings_env(monkeypatch) -> None:
    """The cloud path: CREDENTIALS_ENCRYPTION_KEY set as an app setting; no
    keyring anywhere near the process."""
    _engine, factory = make_engine_and_session()
    monkeypatch.setattr(
        settings, "credentials_encryption_key", Fernet.generate_key().decode()
    )
    monkeypatch.setattr(
        CredentialsStore, "_key_from_keyring", lambda self: pytest.fail(
            "keyring must not be consulted when the env key is set"
        ),
    )
    store = CredentialsStore(session_factory=factory)
    store.store_credentials(7, "eduard", Credentials(username="u", password="p"))
    got = store.get_credentials(7, "eduard")
    assert got is not None and got.password == "p"


def test_keyring_fallback_still_works_for_local_dev(monkeypatch) -> None:
    _engine, factory = make_engine_and_session()
    monkeypatch.setattr(settings, "credentials_encryption_key", "")
    fake_keyring = _install_fake_keyring(monkeypatch)
    store = CredentialsStore(session_factory=factory)
    store.store_credentials(8, "eduard", Credentials(username="u", password="p"))
    got = store.get_credentials(8, "eduard")
    assert got is not None and got.password == "p"
    # The generated key was persisted to the (fake) keyring for next time.
    assert len(fake_keyring) == 1


def test_no_key_anywhere_gives_instructive_error(monkeypatch) -> None:
    """No env key + no keyring (the cloud container before setup) must name
    the exact app setting to add — not the old cryptic keyring error, and
    never a silent plaintext fallback."""
    _engine, factory = make_engine_and_session()
    monkeypatch.setattr(settings, "credentials_encryption_key", "")
    monkeypatch.setattr(CredentialsStore, "_key_from_keyring", lambda self: None)
    store = CredentialsStore(session_factory=factory)
    with pytest.raises(CredentialsStoreError) as excinfo:
        store.store_credentials(
            1, "eduard", Credentials(username="a", password="b")
        )
    message = str(excinfo.value)
    assert "CREDENTIALS_ENCRYPTION_KEY" in message
    assert "One-time" in message
    assert "requires OS keyring" not in message
    # Nothing was written.
    with factory() as db:
        assert db.get(PortalCredential, (1, "eduard")) is None


def test_broken_keyring_without_env_key_gives_instructive_error(
    monkeypatch,
) -> None:
    _engine, factory = make_engine_and_session()
    monkeypatch.setattr(settings, "credentials_encryption_key", "")
    _install_fake_keyring(monkeypatch, working=False)
    store = CredentialsStore(session_factory=factory)
    with pytest.raises(CredentialsStoreError) as excinfo:
        store.store_credentials(
            1, "eduard", Credentials(username="a", password="b")
        )
    assert "CREDENTIALS_ENCRYPTION_KEY" in str(excinfo.value)


def test_malformed_key_gives_instructive_error() -> None:
    store, _ = _make_store(key="not-a-valid-fernet-key")
    with pytest.raises(CredentialsStoreError) as excinfo:
        store.store_credentials(
            1, "eduard", Credentials(username="a", password="b")
        )
    assert "CREDENTIALS_ENCRYPTION_KEY" in str(excinfo.value)
    assert "Fernet" in str(excinfo.value)


def test_wrong_rotated_key_fails_safe_without_plaintext() -> None:
    """Decrypting with a different key than the one used to store must raise
    a clean, explanatory error — no crash, no plaintext leak."""
    _engine, factory = make_engine_and_session()
    original = CredentialsStore(
        session_factory=factory, encryption_key=Fernet.generate_key().decode()
    )
    original.store_credentials(
        4, "eduard", Credentials(username="bob", password="TOPSECRETPW")
    )
    rotated = CredentialsStore(
        session_factory=factory, encryption_key=Fernet.generate_key().decode()
    )
    with pytest.raises(CredentialsStoreError) as excinfo:
        rotated.get_credentials(4, "eduard")
    message = str(excinfo.value)
    assert "TOPSECRETPW" not in message
    assert "CREDENTIALS_ENCRYPTION_KEY" in message


# --- secrets never in logs ----------------------------------------------------


def test_secret_values_never_logged() -> None:
    store, _ = _make_store()
    with capture_logs() as logs:
        store.store_credentials(
            6,
            "eduard",
            Credentials(
                username="logme", password="NEVERLOGGED", email="a@b.com"
            ),
            platform_slug="proactis",
        )
        got = store.get_credentials(6, "eduard")
        store.delete_credentials(6, "eduard")
    assert got is not None and got.password == "NEVERLOGGED"
    dumped = json.dumps(logs, default=str)
    assert "NEVERLOGGED" not in dumped
    assert "logme" not in dumped


# --- generic secret crypto (email token store path) ---------------------------


def test_encrypt_decrypt_secret_roundtrip_and_fail_safe() -> None:
    _engine, factory = make_engine_and_session()
    key_a = Fernet.generate_key().decode()
    store = CredentialsStore(session_factory=factory, encryption_key=key_a)
    blob = store.encrypt_secret("oauth-token-value")
    assert b"oauth-token-value" not in blob
    assert store.decrypt_secret(blob) == "oauth-token-value"

    other = CredentialsStore(
        session_factory=factory, encryption_key=Fernet.generate_key().decode()
    )
    with pytest.raises(CredentialsStoreError):
        other.decrypt_secret(blob)
