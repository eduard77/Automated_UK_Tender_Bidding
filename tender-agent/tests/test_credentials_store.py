"""CredentialsStore — encryption roundtrip + lifecycle. Keyring is mocked
(in-memory) so no OS keyring is touched; cryptography (Fernet) is real.
"""
from __future__ import annotations

import sys
from types import ModuleType

import pytest

from tender_agent.services.credentials import (
    CredentialsStore,
    CredentialsStoreError,
)
from tender_agent.services.portals.base import Credentials


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


def test_store_and_get_roundtrip(tmp_path, monkeypatch) -> None:
    _install_fake_keyring(monkeypatch)
    store = CredentialsStore(db_path=str(tmp_path / "creds.db"))
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


def test_password_not_stored_in_plaintext(tmp_path, monkeypatch) -> None:
    _install_fake_keyring(monkeypatch)
    db_file = tmp_path / "creds.db"
    store = CredentialsStore(db_path=str(db_file))
    store.store_credentials(
        11, "eduard", Credentials(username="bob", password="HUNTER2PLAIN")
    )
    raw = db_file.read_bytes()
    assert b"HUNTER2PLAIN" not in raw


def test_list_returns_metadata_only(tmp_path, monkeypatch) -> None:
    _install_fake_keyring(monkeypatch)
    store = CredentialsStore(db_path=str(tmp_path / "creds.db"))
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


def test_mark_invalid_and_validated(tmp_path, monkeypatch) -> None:
    _install_fake_keyring(monkeypatch)
    store = CredentialsStore(db_path=str(tmp_path / "creds.db"))
    store.store_credentials(5, "eduard", Credentials(username="x", password="y"))
    store.mark_invalid(5, "eduard")
    assert store.list_credentials("eduard")[0].valid is False
    store.mark_validated(5, "eduard")
    meta = store.list_credentials("eduard")[0]
    assert meta.valid is True
    assert meta.last_validated_at is not None


def test_soft_delete_hides_from_list_and_get(tmp_path, monkeypatch) -> None:
    _install_fake_keyring(monkeypatch)
    store = CredentialsStore(db_path=str(tmp_path / "creds.db"))
    store.store_credentials(9, "eduard", Credentials(username="x", password="y"))
    store.delete_credentials(9, "eduard")
    assert store.list_credentials("eduard") == []
    assert store.get_credentials(9, "eduard") is None


def test_keyring_unavailable_raises(tmp_path, monkeypatch) -> None:
    _install_fake_keyring(monkeypatch, working=False)
    store = CredentialsStore(db_path=str(tmp_path / "creds.db"))
    with pytest.raises(CredentialsStoreError):
        store.store_credentials(1, "eduard", Credentials(username="a", password="b"))
