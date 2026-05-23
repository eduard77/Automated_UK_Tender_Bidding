"""Path-safety: every document/staging path derives from the configured,
absolute DOCUMENT_STORAGE_DIR — never a bare relative 'data' path (issue 4d-1).

Covers the orchestrator staging dir (_dest_dir) and the Delta adapter's on-disk
ingest, asserting writes land ONLY under the fake storage root and that no
relative 'data' directory is ever created. Fully mocked — no bridge, no network.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from tender_agent.services.bridge_client import BridgeFile, RenderedPage
from tender_agent.services.portals.adapters import delta_esourcing as delta_mod
from tender_agent.services.portals.adapters.delta_esourcing import (
    DeltaEsourcingAdapter,
    _ingest_bridge_file,
)
from tender_agent.services.portals.base import PortalContext
from tender_agent.services.portals.results import DownloadStatus

STAGE_ONE_URL = (
    "https://www.delta-esourcing.com/delta/suppliers/select/"
    "suppRespStatus.html?id=555&listId=777"
)


# --- orchestrator staging dir ------------------------------------------


def test_dest_dir_derives_from_storage_dir(tmp_path, monkeypatch):
    from tender_agent.services import portal_orchestrator as orch

    monkeypatch.delenv("TENDER_AGENT_DOC_ROOT", raising=False)
    storage = tmp_path / "documents"
    monkeypatch.setattr(orch.settings, "document_storage_dir", str(storage))

    dest = orch._dest_dir(42)

    # Absolute, under the configured storage root, scoped by tender id.
    assert Path(dest).is_absolute()
    assert Path(dest) == storage / "42" / Path(dest).name
    # Never the old bare-relative 'data' path.
    assert not dest.startswith("data")
    assert "tender-documents" not in dest


def test_dest_dir_honours_explicit_root_override(tmp_path, monkeypatch):
    from tender_agent.services import portal_orchestrator as orch

    root = tmp_path / "explicit-root"
    monkeypatch.setenv("TENDER_AGENT_DOC_ROOT", str(root))
    dest = orch._dest_dir(7)
    assert Path(dest).is_absolute()
    assert str(root) in dest


# --- Delta ingest writes only under the storage root -------------------


def _patch_storage(monkeypatch, tmp_path):
    bridge_dl = tmp_path / "bridge-dl"
    bridge_dl.mkdir()
    storage = tmp_path / "storage"
    monkeypatch.setattr(delta_mod.settings, "bridge_download_dir", str(bridge_dl))
    monkeypatch.setattr(delta_mod.settings, "document_storage_dir", str(storage))
    return bridge_dl, storage


def test_ingest_bridge_file_writes_under_storage_only(tmp_path, monkeypatch):
    bridge_dl, storage = _patch_storage(monkeypatch, tmp_path)
    (bridge_dl / "downloaded.pdf").write_bytes(b"%PDF body")
    bf = BridgeFile(path="downloaded.pdf", size_bytes=9, mime_type="application/pdf")

    df = _ingest_bridge_file(
        bf,
        "https://www.delta-esourcing.com/.../downloadDocument.html?docId=1",
        tender_id=900,
        preferred_name="Spec.pdf",
    )

    assert df is not None
    target = Path(df.path).resolve()
    # The persisted file lives strictly under the configured storage root.
    assert target.is_file()
    assert storage.resolve() in target.parents
    # storage_key is relative and traversal-free.
    assert not Path(df.storage_key).is_absolute()
    assert ".." not in df.storage_key
    assert (storage / df.storage_key).resolve() == target


class _DownloadOnlyBridge:
    """Minimal fake bridge for the Delta download step — records navigation and
    writes each 'downloaded' file into the (fake) bridge download dir."""

    def __init__(self, *, html: str, download_dir: str):
        self.html = html
        self.download_dir = download_dir
        self.navigated: list[str] = []

    async def navigate(self, slug, url):
        self.navigated.append(url)
        return {"current_url": STAGE_ONE_URL}

    async def session_status(self, slug):
        return {"current_url": STAGE_ONE_URL}

    async def page_html(self, slug):
        return self.html

    async def rendered_html(
        self, slug, *, wait_for_selector=None, wait_for_text=None, timeout_ms=15000
    ):
        # The Delta adapter reads the JS-rendered documents table via this; the
        # fake "renders" self.html and reports the wait satisfied when the marker
        # is present.
        satisfied = wait_for_text is None or wait_for_text in (self.html or "")
        return RenderedPage(
            html=self.html, wait_satisfied=satisfied, current_url=STAGE_ONE_URL
        )

    async def element_exists(self, slug, selector):
        return False

    async def find_links(self, slug, pattern):
        return []

    async def download(self, slug, url, dest_filename=None):
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", dest_filename or url.split("docId=")[-1])
        p = Path(self.download_dir) / safe
        p.write_bytes(b"%PDF fake " + url.encode())
        return BridgeFile(path=safe, size_bytes=p.stat().st_size, mime_type="application/pdf")


def _docs_table_html(n=2, resp_id="555", list_id="777"):
    rows = []
    for i in range(n):
        href = (
            "/delta/suppliers/response/overview/documents/downloadDocument.html"
            f"?respId={resp_id}&supplierListId={list_id}&docId={1000 + i}"
        )
        rows.append(
            f"<tr><td>Document {i}.pdf</td><td>1 MB</td><td>PDF</td>"
            f"<td>01/05/2026</td><td><a href='{href}'>Download</a></td></tr>"
        )
    return "<table>" + "".join(rows) + "</table>"


@pytest.mark.asyncio
async def test_delta_download_writes_only_under_fake_storage(tmp_path, monkeypatch):
    bridge_dl, storage = _patch_storage(monkeypatch, tmp_path)
    bridge = _DownloadOnlyBridge(html=_docs_table_html(n=2), download_dir=str(bridge_dl))
    ctx = PortalContext(
        portal_id=1,
        user_id="tester",
        domain="delta-esourcing.com",
        bridge=bridge,
        platform_slug="delta_esourcing",
        tender_id=900,
    )

    # Run from a scratch CWD so we can prove the code never created a bare
    # relative 'data' directory next to the working directory.
    scratch = tmp_path / "cwd"
    scratch.mkdir()
    monkeypatch.chdir(scratch)

    res = await DeltaEsourcingAdapter().download_documents(ctx, "ignored-dest")

    assert res.status == DownloadStatus.complete
    assert len(res.files) == 2
    # Every persisted file is under the fake storage root, nowhere else.
    for f in res.files:
        target = Path(f.path).resolve()
        assert storage.resolve() in target.parents
        assert (storage / f.storage_key).resolve() == target
    # No bare-relative 'data' directory was created in the working directory.
    assert not (scratch / "data").exists()
