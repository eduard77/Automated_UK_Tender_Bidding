"""Blob storage abstraction for vault documents.

Phase 3 ships `LocalVaultStorage` (writes to `settings.document_storage_dir`).
The production `S3VaultStorage` lands at deploy time and slots in behind the
same `VaultStorage` interface — see PROJECT.md §5.4 ("storage_key: str" path
shape) and docs/deploy.md.

Storage keys follow:
    vault/{org_id}/{document_id}/{version}.{ext}

so a future S3 layout reads identically and `storage_key` works as the key
under either backend.
"""
from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

import structlog

from tender_agent.config import settings

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class StoredBlob:
    """What the storage layer hands back after writing."""

    storage_key: str
    bytes: int
    sha256: str


class VaultStorage(ABC):
    """Backend-agnostic blob store. Implementations: LocalVaultStorage (dev),
    S3VaultStorage (prod — TBD)."""

    @abstractmethod
    def save(
        self, *, org_id: int, document_id: int, version: int, ext: str, content: bytes
    ) -> StoredBlob: ...

    @abstractmethod
    def read(self, storage_key: str) -> bytes: ...

    @abstractmethod
    def exists(self, storage_key: str) -> bool: ...


class LocalVaultStorage(VaultStorage):
    """Disk-backed storage under `settings.document_storage_dir`. The layout
    mirrors what S3 will look like, so the storage_key works in either place."""

    def __init__(self, root: str | None = None) -> None:
        self._root = Path(root or settings.document_storage_dir) / "vault"
        self._root.mkdir(parents=True, exist_ok=True)

    def save(
        self, *, org_id: int, document_id: int, version: int, ext: str, content: bytes
    ) -> StoredBlob:
        key = f"vault/{org_id}/{document_id}/{version}.{ext.lstrip('.')}"
        path = self._root.parent / key  # _root already ends in /vault
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        sha = hashlib.sha256(content).hexdigest()
        logger.info(
            "vault.storage.saved",
            storage_key=key,
            bytes=len(content),
            sha256=sha,
        )
        return StoredBlob(storage_key=key, bytes=len(content), sha256=sha)

    def read(self, storage_key: str) -> bytes:
        path = self._root.parent / storage_key
        return path.read_bytes()

    def exists(self, storage_key: str) -> bool:
        return (self._root.parent / storage_key).exists()


_DEFAULT: VaultStorage | None = None


def get_storage() -> VaultStorage:
    """Return the configured vault storage backend.

    The default factory returns a `LocalVaultStorage`. Tests and the future
    S3-backed deploy override this via `set_storage()`.
    """
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = LocalVaultStorage()
    return _DEFAULT


def set_storage(storage: VaultStorage) -> None:
    """Override the module-level storage. Used by tests and by production
    bootstrap (where the S3 backend is wired before the first request)."""
    global _DEFAULT
    _DEFAULT = storage
