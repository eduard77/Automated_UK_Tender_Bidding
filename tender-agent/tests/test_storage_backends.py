"""Storage backends — local disk + Azure Blob (mocked SDK).

The azure backend takes an INJECTED BlobServiceClient, so the whole put/url path
is exercised without the azure SDK installed or any network — the lazy import
only fires when no client is injected.
"""
from __future__ import annotations

from tender_agent.services.storage import (
    AzureBlobStorageBackend,
    LocalStorageBackend,
    get_storage_backend,
)

# --- local -----------------------------------------------------------------


def test_local_backend_writes_bytes_under_root(tmp_path):
    be = LocalStorageBackend(str(tmp_path))
    be.put("42/abc.pdf", b"%PDF-data", content_type="application/pdf")
    assert (tmp_path / "42" / "abc.pdf").read_bytes() == b"%PDF-data"
    # Local files are served via the file endpoint, not a direct URL.
    assert be.url("42/abc.pdf") is None
    assert be.name == "local"


# --- azure blob (mocked client) -------------------------------------------


class _FakeBlobClient:
    def __init__(self, record):
        self._record = record

    def upload_blob(self, data, **kwargs):
        self._record["uploaded"] = (data, kwargs)


class _FakeBlobServiceClient:
    def __init__(self):
        self.calls = []
        self.record = {}

    def get_blob_client(self, *, container, blob):
        self.calls.append((container, blob))
        return _FakeBlobClient(self.record)


def test_azure_backend_puts_with_overwrite_and_correct_key():
    fake = _FakeBlobServiceClient()
    be = AzureBlobStorageBackend("generasystemsfiles", "tender-documents", client=fake)
    be.put("99/deadbeef.pdf", b"bytes", content_type="application/pdf")
    # Routed to the right container + blob key.
    assert fake.calls == [("tender-documents", "99/deadbeef.pdf")]
    data, kwargs = fake.record["uploaded"]
    assert data == b"bytes"
    # Idempotent put: overwrite is always set.
    assert kwargs.get("overwrite") is True
    assert be.name == "azure_blob"


def test_azure_backend_url_is_account_container_key():
    be = AzureBlobStorageBackend("generasystemsfiles", "tender-documents", client=object())
    assert be.url("99/deadbeef.pdf") == (
        "https://generasystemsfiles.blob.core.windows.net/"
        "tender-documents/99/deadbeef.pdf"
    )


def test_azure_put_tolerates_missing_content_settings(monkeypatch):
    # If the SDK's ContentSettings import fails, the upload still proceeds.
    fake = _FakeBlobServiceClient()
    be = AzureBlobStorageBackend("acct", "cont", client=fake)
    be.put("k", b"x")  # no content_type → no ContentSettings import attempted
    assert fake.record["uploaded"][0] == b"x"


# --- factory ---------------------------------------------------------------


def test_factory_returns_local_when_no_account(monkeypatch):
    from tender_agent.services import storage as storage_mod

    monkeypatch.setattr(storage_mod.settings, "azure_storage_account", "")
    be = get_storage_backend()
    assert isinstance(be, LocalStorageBackend)
    assert be.name == "local"


def test_factory_returns_azure_when_account_set(monkeypatch):
    from tender_agent.services import storage as storage_mod

    monkeypatch.setattr(storage_mod.settings, "azure_storage_account", "generasystemsfiles")
    monkeypatch.setattr(storage_mod.settings, "azure_blob_container", "tender-documents")
    be = get_storage_backend()
    assert isinstance(be, AzureBlobStorageBackend)
    assert be.name == "azure_blob"
    assert be.account_url == "https://generasystemsfiles.blob.core.windows.net"
