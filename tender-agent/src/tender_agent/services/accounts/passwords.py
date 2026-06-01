"""Password hashing — bcrypt. We never store / log plaintext; only the salted
hash is persisted. Verification is constant-time inside bcrypt.

Kept as a tiny dedicated module so tests can monkey-patch the rounds count
down to bcrypt's minimum (4) for speed without infecting prod settings."""
from __future__ import annotations

import bcrypt

# Production rounds. Tests override `_ROUNDS` to 4 to keep the suite fast;
# don't lower this constant in prod.
_ROUNDS = 12


def hash_password(plain: str) -> str:
    """Hash a plain-text password. Returns the bcrypt hash as a utf-8 string."""
    if not plain:
        raise ValueError("password must not be empty")
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt(_ROUNDS)).decode(
        "utf-8"
    )


def verify_password(plain: str, hashed: str) -> bool:
    """Constant-time verify. False for any malformed hash — never raises."""
    if not plain or not hashed:
        return False
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        # Malformed hash on disk. We don't want a corrupt row to 500 the API.
        return False
