"""Encrypted credentials store for portal logins — cloud-safe.

Storage: the `portal_credentials` table in the MAIN Postgres DB. The
credential payload (username / password / email / extra) is serialised to
JSON and encrypted with Fernet (authenticated symmetric encryption) before it
is written; only non-secret metadata (portal_id, user_id, platform_slug,
valid, timestamps) is stored in the clear so listing never needs to decrypt.

Encryption key — resolved in this order:
  1. The `CREDENTIALS_ENCRYPTION_KEY` setting (env var locally, App Service
     application setting on Azure). The cloud path: ONE secret, set ONCE in
     the Azure portal UI, no terminal needed.
  2. The OS keyring (local-dev convenience only — generated and stored on
     first use, exactly as before). The cloud container has no keyring.
If neither source yields a key the store refuses to operate with an error
that names the app setting and how to generate it. It NEVER falls back to
plaintext, and decryption with a wrong/rotated key fails safe with a clear
error rather than crashing or leaking.

This replaced the original standalone-SQLite-plus-keyring design: the Azure
container has neither an OS keyring nor a persistent local disk, so that
store could not hold anything on the cloud. A local dev who had logins in
`~/.tender-agent/credentials.db` re-adds them via POST /credentials.

The email OAuth token store (services/email/token_store.py) reuses
`encrypt_secret` / `decrypt_secret` below, so the same single key covers
email tokens — no second secret mechanism.
"""
from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select

from tender_agent.config import settings
from tender_agent.models import PortalCredential
from tender_agent.services.portals.base import Credentials

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = structlog.get_logger(__name__)

KEYRING_SERVICE = "tender-agent"
KEYRING_KEY_NAME = "credentials-db-key"

_GENERATE_KEY_HINT = (
    'python -c "from cryptography.fernet import Fernet; '
    'print(Fernet.generate_key().decode())"'
)

NO_KEY_ERROR_MESSAGE = (
    "Credentials store has no encryption key. One-time setup: generate a key "
    f"with {_GENERATE_KEY_HINT} and add it as an application setting named "
    "CREDENTIALS_ENCRYPTION_KEY (Azure portal -> the App Service -> Settings "
    "-> Environment variables -> Add), then restart the app. Locally, set "
    "CREDENTIALS_ENCRYPTION_KEY in .env instead (or configure an OS keyring)."
)

INVALID_KEY_ERROR_MESSAGE = (
    "CREDENTIALS_ENCRYPTION_KEY is set but is not a valid Fernet key. "
    f"Generate a valid one with {_GENERATE_KEY_HINT} and update the "
    "CREDENTIALS_ENCRYPTION_KEY application setting."
)

DECRYPT_FAILED_ERROR_MESSAGE = (
    "Stored secret could not be decrypted with the current encryption key — "
    "CREDENTIALS_ENCRYPTION_KEY has most likely changed since the secret was "
    "stored. Restore the original key, or delete and re-add the credential."
)


class CredentialsStoreError(RuntimeError):
    """Raised when the store cannot operate (no key, bad key, wrong key)."""


@dataclass
class CredentialMetadata:
    portal_id: int
    platform_slug: str | None
    valid: bool
    last_used_at: str | None
    last_validated_at: str | None


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


class CredentialsStore:
    """Public interface is unchanged from the legacy store: callers keep
    using store/get/mark_*/list/delete and encrypt_secret/decrypt_secret.
    Only the backend moved (SQLite file -> Postgres column) and the key
    source gained the env-var path."""

    def __init__(
        self,
        session_factory: Callable[[], Session] | None = None,
        encryption_key: str | None = None,
    ) -> None:
        # Both lazily resolved so importing this module never touches the DB
        # or the key material. Tests inject an in-memory session factory and
        # an explicit key.
        self._session_factory = session_factory
        self._explicit_key = encryption_key
        self._fernet: Fernet | None = None

    # --- key resolution --------------------------------------------------

    def _key_from_keyring(self) -> bytes | None:
        """Local-dev fallback: read (or generate-and-store) a key in the OS
        keyring. Returns None when no usable keyring backend exists — the
        caller raises the instructive no-key error instead."""
        try:
            import keyring
        except Exception:  # noqa: BLE001 - keyring optional
            return None

        try:
            from keyring.backends.fail import Keyring as FailKeyring

            if isinstance(keyring.get_keyring(), FailKeyring):
                return None
        except Exception:  # noqa: BLE001 - probing the backend; ignore
            pass

        try:
            existing = keyring.get_password(KEYRING_SERVICE, KEYRING_KEY_NAME)
            if existing:
                return existing.encode("utf-8")
            key = Fernet.generate_key()
            keyring.set_password(
                KEYRING_SERVICE, KEYRING_KEY_NAME, key.decode("utf-8")
            )
            return key
        except Exception:  # noqa: BLE001 - any keyring failure => no key
            return None

    def _resolve_key(self) -> bytes:
        if self._explicit_key:
            return self._explicit_key.strip().encode("utf-8")
        configured = settings.credentials_encryption_key.strip()
        if configured:
            return configured.encode("utf-8")
        from_keyring = self._key_from_keyring()
        if from_keyring:
            return from_keyring
        raise CredentialsStoreError(NO_KEY_ERROR_MESSAGE)

    def _ensure_fernet(self) -> Fernet:
        if self._fernet is None:
            key = self._resolve_key()
            try:
                self._fernet = Fernet(key)
            except (ValueError, TypeError) as exc:
                raise CredentialsStoreError(INVALID_KEY_ERROR_MESSAGE) from exc
        return self._fernet

    # --- DB session ------------------------------------------------------

    @contextmanager
    def _session(self) -> Iterator[Session]:
        if self._session_factory is None:
            from tender_agent.db import SessionLocal

            self._session_factory = SessionLocal
        db = self._session_factory()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    # --- crypto ----------------------------------------------------------

    def _encrypt(self, creds: Credentials) -> bytes:
        payload = json.dumps(asdict(creds)).encode("utf-8")
        return self._ensure_fernet().encrypt(payload)

    def _decrypt(self, blob: bytes) -> Credentials:
        try:
            raw = self._ensure_fernet().decrypt(bytes(blob))
        except InvalidToken as exc:
            raise CredentialsStoreError(DECRYPT_FAILED_ERROR_MESSAGE) from exc
        data = json.loads(raw.decode("utf-8"))
        return Credentials(
            username=data.get("username"),
            password=data.get("password"),
            email=data.get("email"),
            extra=data.get("extra") or {},
        )

    # --- generic secret crypto (reused by the email OAuth token store) ----
    # The email integration stores OAuth tokens encrypted at rest with this
    # store's key rather than introducing a second secret mechanism. The
    # token ciphertext lives on the per-account mailbox_accounts row; these
    # helpers expose just the crypto. See services/email/token_store.py.

    def encrypt_secret(self, plaintext: str) -> bytes:
        """Fernet-encrypt an arbitrary secret string. Never logs the value."""
        return self._ensure_fernet().encrypt(plaintext.encode("utf-8"))

    def decrypt_secret(self, blob: bytes) -> str:
        """Decrypt a value produced by `encrypt_secret`. A wrong/rotated key
        fails safe with CredentialsStoreError, never a stack-trace crash."""
        try:
            return self._ensure_fernet().decrypt(bytes(blob)).decode("utf-8")
        except InvalidToken as exc:
            raise CredentialsStoreError(DECRYPT_FAILED_ERROR_MESSAGE) from exc

    # --- operations --------------------------------------------------------

    def store_credentials(
        self,
        portal_id: int,
        user_id: str,
        creds: Credentials,
        platform_slug: str | None = None,
    ) -> None:
        """Upsert credentials for (portal_id, user_id). Resets valid=true and
        clears any prior soft-delete."""
        secret = self._encrypt(creds)  # resolve the key before any DB write
        now = _utcnow()
        with self._session() as db:
            row = db.get(PortalCredential, (portal_id, user_id))
            if row is None:
                row = PortalCredential(
                    portal_id=portal_id, user_id=user_id, created_at=now
                )
                db.add(row)
            row.platform_slug = platform_slug
            row.secret_ciphertext = secret
            row.valid = True
            row.deleted_at = None
            row.updated_at = now
        # Audit: metadata only — NEVER the credential values.
        logger.info(
            "credentials.stored", portal_id=portal_id, user_id=user_id
        )

    def get_credentials(
        self, portal_id: int, user_id: str
    ) -> Credentials | None:
        with self._session() as db:
            row = db.get(PortalCredential, (portal_id, user_id))
            if row is None or row.deleted_at is not None:
                return None
            blob = row.secret_ciphertext
        return self._decrypt(blob)

    def mark_invalid(self, portal_id: int, user_id: str) -> None:
        with self._session() as db:
            row = db.get(PortalCredential, (portal_id, user_id))
            if row is None:
                return
            row.valid = False
            row.updated_at = _utcnow()

    def mark_validated(self, portal_id: int, user_id: str) -> None:
        now = _utcnow()
        with self._session() as db:
            row = db.get(PortalCredential, (portal_id, user_id))
            if row is None:
                return
            row.valid = True
            row.last_validated_at = now
            row.updated_at = now

    def mark_used(self, portal_id: int, user_id: str) -> None:
        now = _utcnow()
        with self._session() as db:
            row = db.get(PortalCredential, (portal_id, user_id))
            if row is None:
                return
            row.last_used_at = now
            row.updated_at = now

    def list_credentials(self, user_id: str) -> list[CredentialMetadata]:
        with self._session() as db:
            rows = (
                db.execute(
                    select(PortalCredential)
                    .where(
                        PortalCredential.user_id == user_id,
                        PortalCredential.deleted_at.is_(None),
                    )
                    .order_by(PortalCredential.portal_id)
                )
                .scalars()
                .all()
            )
            return [
                CredentialMetadata(
                    portal_id=row.portal_id,
                    platform_slug=row.platform_slug,
                    valid=bool(row.valid),
                    last_used_at=_iso(row.last_used_at),
                    last_validated_at=_iso(row.last_validated_at),
                )
                for row in rows
            ]

    def delete_credentials(self, portal_id: int, user_id: str) -> None:
        """Soft delete: mark invalid + set deleted_at. The row is retained for
        the audit trail; it disappears from list/get."""
        now = _utcnow()
        with self._session() as db:
            row = db.get(PortalCredential, (portal_id, user_id))
            if row is None:
                return
            row.valid = False
            row.deleted_at = now
            row.updated_at = now
        logger.info(
            "credentials.deleted", portal_id=portal_id, user_id=user_id
        )


# Module-level singleton used by the API. Tests instantiate their own with an
# in-memory session factory + explicit key.
_store: CredentialsStore | None = None


def get_store() -> CredentialsStore:
    global _store
    if _store is None:
        _store = CredentialsStore()
    return _store
