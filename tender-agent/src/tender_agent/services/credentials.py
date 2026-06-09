"""Encrypted credentials store for portal logins.

Design (per design doc): a standalone SQLite database, separate from the main
Postgres app DB, with the encryption key held in the OS keyring (Windows
Credential Manager on the deployment target, Secret Service on Linux, Keychain
on macOS). The credential payload (username / password / email / extra) is
serialised to JSON and encrypted with Fernet before being written; only
non-secret metadata (portal_id, user_id, platform_slug, valid, timestamps) is
stored in the clear so listing never needs to decrypt.

Migrates to AWS Secrets Manager on deployment; this interface stays the same.

Lazy init: the first call generates a fresh key (if none in keyring), stores
it, and creates the schema. If no usable keyring backend is configured the
store refuses to operate with a clear error.
"""
from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from tender_agent.services.portals.base import Credentials

logger = structlog.get_logger(__name__)

KEYRING_SERVICE = "tender-agent"
KEYRING_KEY_NAME = "credentials-db-key"

KEYRING_ERROR_MESSAGE = (
    "Credentials store requires OS keyring. Configure keyring backend "
    "before proceeding."
)


class CredentialsStoreError(RuntimeError):
    """Raised when the store cannot operate (e.g. no keyring backend)."""


@dataclass
class CredentialMetadata:
    portal_id: int
    platform_slug: str | None
    valid: bool
    last_used_at: str | None
    last_validated_at: str | None


def default_db_path() -> str:
    env = os.environ.get("TENDER_AGENT_CREDENTIALS_DB")
    if env:
        return env
    return str(Path.home() / ".tender-agent" / "credentials.db")


def _now() -> str:
    return datetime.now(UTC).isoformat()


class CredentialsStore:
    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path or default_db_path()
        self._fernet: Any | None = None
        self._initialised = False

    # --- lazy init -----------------------------------------------------

    def _get_or_create_key(self) -> bytes:
        import keyring

        # Detect a non-functional backend up front for a clear error.
        try:
            from keyring.backends.fail import Keyring as FailKeyring

            if isinstance(keyring.get_keyring(), FailKeyring):
                raise CredentialsStoreError(KEYRING_ERROR_MESSAGE)
        except CredentialsStoreError:
            raise
        except Exception:  # noqa: BLE001 - probing the backend; ignore
            pass

        try:
            existing = keyring.get_password(KEYRING_SERVICE, KEYRING_KEY_NAME)
        except Exception as exc:  # noqa: BLE001
            raise CredentialsStoreError(KEYRING_ERROR_MESSAGE) from exc

        if existing:
            return existing.encode("utf-8")

        from cryptography.fernet import Fernet

        key = Fernet.generate_key()
        try:
            keyring.set_password(
                KEYRING_SERVICE, KEYRING_KEY_NAME, key.decode("utf-8")
            )
        except Exception as exc:  # noqa: BLE001
            raise CredentialsStoreError(KEYRING_ERROR_MESSAGE) from exc
        return key

    def _ensure_init(self) -> None:
        if self._initialised:
            return
        from cryptography.fernet import Fernet

        key = self._get_or_create_key()
        self._fernet = Fernet(key)

        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS credentials (
                    portal_id INTEGER NOT NULL,
                    user_id TEXT NOT NULL,
                    platform_slug TEXT,
                    secret BLOB NOT NULL,
                    valid INTEGER NOT NULL DEFAULT 1,
                    last_used_at TEXT,
                    last_validated_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    deleted_at TEXT,
                    PRIMARY KEY (portal_id, user_id)
                )
                """
            )
            conn.commit()
        self._initialised = True

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        # sqlite3's own context manager only manages the transaction, not the
        # connection lifetime — leaking connections holds a file lock on
        # Windows. Close explicitly here.
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # --- crypto --------------------------------------------------------

    def _encrypt(self, creds: Credentials) -> bytes:
        assert self._fernet is not None
        payload = json.dumps(asdict(creds)).encode("utf-8")
        return self._fernet.encrypt(payload)

    def _decrypt(self, blob: bytes) -> Credentials:
        assert self._fernet is not None
        raw = self._fernet.decrypt(blob)
        data = json.loads(raw.decode("utf-8"))
        return Credentials(
            username=data.get("username"),
            password=data.get("password"),
            email=data.get("email"),
            extra=data.get("extra") or {},
        )

    # --- generic secret crypto (reused by the email OAuth token store) ------
    # The email integration stores OAuth tokens encrypted at rest, reusing this
    # store's keyring-held Fernet key rather than introducing a second secret
    # mechanism. The token ciphertext lives on the per-account mailbox_accounts
    # row (Postgres), so these helpers expose just the crypto, not the SQLite
    # credentials table. See services/email/token_store.py.

    def encrypt_secret(self, plaintext: str) -> bytes:
        """Fernet-encrypt an arbitrary secret string. Never logs the value."""
        self._ensure_init()
        assert self._fernet is not None
        return self._fernet.encrypt(plaintext.encode("utf-8"))

    def decrypt_secret(self, blob: bytes) -> str:
        """Decrypt a value produced by `encrypt_secret`."""
        self._ensure_init()
        assert self._fernet is not None
        return self._fernet.decrypt(blob).decode("utf-8")

    # --- operations ----------------------------------------------------

    def store_credentials(
        self,
        portal_id: int,
        user_id: str,
        creds: Credentials,
        platform_slug: str | None = None,
    ) -> None:
        """Upsert credentials for (portal_id, user_id). Resets valid=true and
        clears any prior soft-delete."""
        self._ensure_init()
        secret = self._encrypt(creds)
        now = _now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO credentials (
                    portal_id, user_id, platform_slug, secret, valid,
                    created_at, updated_at, deleted_at
                ) VALUES (?, ?, ?, ?, 1, ?, ?, NULL)
                ON CONFLICT(portal_id, user_id) DO UPDATE SET
                    platform_slug = excluded.platform_slug,
                    secret = excluded.secret,
                    valid = 1,
                    updated_at = excluded.updated_at,
                    deleted_at = NULL
                """,
                (portal_id, user_id, platform_slug, secret, now, now),
            )
            conn.commit()

    def get_credentials(
        self, portal_id: int, user_id: str
    ) -> Credentials | None:
        self._ensure_init()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT secret FROM credentials
                WHERE portal_id = ? AND user_id = ? AND deleted_at IS NULL
                """,
                (portal_id, user_id),
            ).fetchone()
        if row is None:
            return None
        return self._decrypt(row["secret"])

    def mark_invalid(self, portal_id: int, user_id: str) -> None:
        self._ensure_init()
        with self._connect() as conn:
            conn.execute(
                "UPDATE credentials SET valid = 0, updated_at = ? "
                "WHERE portal_id = ? AND user_id = ?",
                (_now(), portal_id, user_id),
            )
            conn.commit()

    def mark_validated(self, portal_id: int, user_id: str) -> None:
        self._ensure_init()
        now = _now()
        with self._connect() as conn:
            conn.execute(
                "UPDATE credentials SET valid = 1, last_validated_at = ?, "
                "updated_at = ? WHERE portal_id = ? AND user_id = ?",
                (now, now, portal_id, user_id),
            )
            conn.commit()

    def mark_used(self, portal_id: int, user_id: str) -> None:
        self._ensure_init()
        now = _now()
        with self._connect() as conn:
            conn.execute(
                "UPDATE credentials SET last_used_at = ?, updated_at = ? "
                "WHERE portal_id = ? AND user_id = ?",
                (now, now, portal_id, user_id),
            )
            conn.commit()

    def list_credentials(self, user_id: str) -> list[CredentialMetadata]:
        self._ensure_init()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT portal_id, platform_slug, valid, last_used_at,
                       last_validated_at
                FROM credentials
                WHERE user_id = ? AND deleted_at IS NULL
                ORDER BY portal_id
                """,
                (user_id,),
            ).fetchall()
        return [
            CredentialMetadata(
                portal_id=row["portal_id"],
                platform_slug=row["platform_slug"],
                valid=bool(row["valid"]),
                last_used_at=row["last_used_at"],
                last_validated_at=row["last_validated_at"],
            )
            for row in rows
        ]

    def delete_credentials(self, portal_id: int, user_id: str) -> None:
        """Soft delete: mark invalid + set deleted_at. The row is retained for
        the audit trail; it disappears from list/get."""
        self._ensure_init()
        now = _now()
        with self._connect() as conn:
            conn.execute(
                "UPDATE credentials SET valid = 0, deleted_at = ?, "
                "updated_at = ? WHERE portal_id = ? AND user_id = ?",
                (now, now, portal_id, user_id),
            )
            conn.commit()


# Module-level singleton used by the API. Tests instantiate their own with a
# tmp db_path + mocked keyring.
_store: CredentialsStore | None = None


def get_store() -> CredentialsStore:
    global _store
    if _store is None:
        _store = CredentialsStore()
    return _store
