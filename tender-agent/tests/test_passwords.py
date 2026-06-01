"""Password hashing/verification — bcrypt round-trip + safety on malformed input."""
from __future__ import annotations

import pytest

from tender_agent.services.accounts import passwords


@pytest.fixture(autouse=True)
def _fast_bcrypt(monkeypatch) -> None:
    # bcrypt minimum cost is 4 — keep the suite fast.
    monkeypatch.setattr(passwords, "_ROUNDS", 4)


def test_hash_and_verify_round_trip() -> None:
    h = passwords.hash_password("Correct Horse Battery Staple")
    assert h.startswith("$2b$")
    assert passwords.verify_password("Correct Horse Battery Staple", h)


def test_verify_rejects_wrong_password() -> None:
    h = passwords.hash_password("rightpass")
    assert passwords.verify_password("wrongpass", h) is False


def test_verify_rejects_empty_inputs() -> None:
    h = passwords.hash_password("anypass")
    assert passwords.verify_password("", h) is False
    assert passwords.verify_password("anypass", "") is False


def test_verify_does_not_raise_on_malformed_hash() -> None:
    # Corrupt rows shouldn't 500 the API — verify returns False.
    assert passwords.verify_password("anypass", "not-a-bcrypt-hash") is False


def test_hash_password_rejects_empty() -> None:
    with pytest.raises(ValueError):
        passwords.hash_password("")
