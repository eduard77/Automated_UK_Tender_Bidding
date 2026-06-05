"""Pluggable document-bytes storage — local disk and Azure Blob.

The original fetch pipeline wrote document bytes straight to local disk and
hard-coded `storage_backend="local"`. The local-fetch pivot pushes documents to
the cloud, where the bytes must live in Azure Blob instead. This module is the
seam: a tiny `StorageBackend` protocol with two implementations, picked by
`get_storage_backend()` from config.

Azure auth is MANAGED IDENTITY only (DefaultAzureCredential) — NO connection
string. In Azure the App Service's system-assigned identity (granted "Storage
Blob Data Contributor" on the account) is used; locally DefaultAzureCredential
falls back to dev credentials. The azure SDK is imported lazily so a plain
checkout / CI without the SDK (or without Azure) still imports this module and
uses the local backend.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import structlog

from tender_agent.config import settings

logger = structlog.get_logger(__name__)


@runtime_checkable
class StorageBackend(Protocol):
    """Stores raw document bytes under a backend-defined key.

    `name` is what lands in `TenderDocumentFile.storage_backend` (e.g. "local",
    "azure_blob"). `put` is idempotent — re-putting the same key overwrites with
    identical bytes. `url` returns a retrievable address when the backend has one
    (Azure Blob URL), else None (local disk is served via the file endpoint)."""

    name: str

    def put(self, key: str, data: bytes, *, content_type: str | None = None) -> None: ...
    def url(self, key: str) -> str | None: ...


class LocalStorageBackend:
    """Writes bytes under `root/<key>` — the pre-pivot behaviour, kept so a local
    or CI run (no Azure) still works end-to-end."""

    name = "local"

    def __init__(self, root: str) -> None:
        self._root = Path(root)

    def put(self, key: str, data: bytes, *, content_type: str | None = None) -> None:
        path = self._root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def url(self, key: str) -> str | None:
        return None


class AzureBlobStorageBackend:
    """Stores bytes in an Azure Blob container via DefaultAzureCredential
    (managed identity in Azure, dev creds locally). The SDK + credential are
    created lazily on first `put`/`url`, and a client can be injected for tests
    so neither the SDK nor Azure is needed to exercise the call path."""

    name = "azure_blob"

    def __init__(
        self, account: str, container: str, *, client: Any | None = None
    ) -> None:
        self._account = account
        self._container = container
        self._client = client  # an injected BlobServiceClient (tests) or None

    @property
    def account_url(self) -> str:
        return f"https://{self._account}.blob.core.windows.net"

    def _service(self) -> Any:
        if self._client is None:
            # Lazy: only import the SDK + touch Azure when actually used.
            from azure.identity import DefaultAzureCredential  # noqa: PLC0415
            from azure.storage.blob import BlobServiceClient  # noqa: PLC0415

            self._client = BlobServiceClient(
                account_url=self.account_url,
                credential=DefaultAzureCredential(),
            )
        return self._client

    def put(self, key: str, data: bytes, *, content_type: str | None = None) -> None:
        blob = self._service().get_blob_client(container=self._container, blob=key)
        kwargs: dict[str, Any] = {"overwrite": True}
        if content_type:
            # ContentSettings lives in the SDK; skip it gracefully if absent so a
            # content-type hint never blocks the upload.
            try:
                from azure.storage.blob import ContentSettings  # noqa: PLC0415

                kwargs["content_settings"] = ContentSettings(content_type=content_type)
            except Exception:  # noqa: BLE001
                pass
        blob.upload_blob(data, **kwargs)

    def url(self, key: str) -> str | None:
        return f"{self.account_url}/{self._container}/{key}"


def get_storage_backend() -> StorageBackend:
    """Pick the backend from config: Azure Blob when an account is configured,
    else local disk. Built per request — cheap, and the Azure client itself is
    created lazily on first use."""
    if settings.azure_storage_account:
        return AzureBlobStorageBackend(
            settings.azure_storage_account, settings.azure_blob_container
        )
    return LocalStorageBackend(settings.document_storage_dir)


__all__ = [
    "AzureBlobStorageBackend",
    "LocalStorageBackend",
    "StorageBackend",
    "get_storage_backend",
]
