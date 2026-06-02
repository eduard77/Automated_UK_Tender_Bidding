"""Preflight self-diagnosing checks.

Writable dir -> ok; non-writable dir -> reports the problem; missing token ->
reports the problem; bridge unreachable -> reported as down but NOT fatal.
All mocked: no real bridge, no network.
"""
from __future__ import annotations

import pytest

from tender_agent.services import preflight as pf


def _install_fake_bridge_available(monkeypatch, coro_returning_bool):
    """Replace `pf.make_bridge_client` with a factory that returns an object
    whose `bridge_available()` defers to the supplied coroutine. Works for
    either the HTTP or in-process implementation — the test only cares
    about the bool the preflight sees."""

    class _Fake:
        async def bridge_available(self):
            return await coro_returning_bool(self)

    monkeypatch.setattr(pf, "make_bridge_client", lambda: _Fake())


# --- documents dir writability -----------------------------------------


def test_documents_dir_writable_ok(tmp_path, monkeypatch):
    storage = tmp_path / "documents"
    monkeypatch.setattr(pf.settings, "document_storage_dir", str(storage))
    ok, detail = pf.check_documents_dir_writable()
    assert ok is True
    assert str(storage) in detail
    # The probe file is cleaned up — only the directory remains.
    assert storage.is_dir()
    assert list(storage.iterdir()) == []


def test_documents_dir_not_writable_reports_problem(tmp_path, monkeypatch):
    # Point the storage dir *through* an existing file: mkdir can't create a
    # directory under a file, so the check fails the same way a read-only mount
    # would — without needing real filesystem permissions (cross-platform).
    a_file = tmp_path / "not-a-dir"
    a_file.write_bytes(b"x")
    monkeypatch.setattr(pf.settings, "document_storage_dir", str(a_file / "sub"))
    ok, detail = pf.check_documents_dir_writable()
    assert ok is False
    assert "not writable" in detail
    assert "DOCUMENT_STORAGE_DIR" in detail


# --- bridge token ------------------------------------------------------


def test_bridge_token_set(monkeypatch):
    monkeypatch.setattr(pf.settings, "bridge_token", "TA-secret")
    ok, detail = pf.check_bridge_token()
    assert ok is True


@pytest.mark.parametrize("value", ["", "   "])
def test_bridge_token_missing_reports_problem(monkeypatch, value):
    monkeypatch.setattr(pf.settings, "bridge_token", value)
    ok, detail = pf.check_bridge_token()
    assert ok is False
    assert "TENDER_AGENT_BRIDGE_TOKEN" in detail


# --- bridge reachability (best-effort, never fatal) --------------------


@pytest.mark.asyncio
async def test_bridge_reachable_up(monkeypatch):
    async def _up(self):
        return True

    _install_fake_bridge_available(monkeypatch, _up)
    ok, detail = await pf.check_bridge_reachable()
    assert ok is True
    assert "reachable" in detail


@pytest.mark.asyncio
async def test_bridge_reachable_down(monkeypatch):
    async def _down(self):
        return False

    _install_fake_bridge_available(monkeypatch, _down)
    ok, detail = await pf.check_bridge_reachable()
    assert ok is False
    assert "not reachable" in detail


# --- run_preflight aggregation -----------------------------------------


@pytest.mark.asyncio
async def test_run_preflight_all_ok(tmp_path, monkeypatch):
    monkeypatch.setattr(pf.settings, "document_storage_dir", str(tmp_path / "d"))
    monkeypatch.setattr(pf.settings, "bridge_token", "TA-secret")

    async def _up(self):
        return True

    _install_fake_bridge_available(monkeypatch, _up)
    res = await pf.run_preflight(check_bridge=True)
    d = res.as_dict()
    assert d["documents_dir_writable"] is True
    assert d["bridge_token_set"] is True
    assert d["bridge_reachable"] is True
    assert len(d["details"]) == 3


@pytest.mark.asyncio
async def test_run_preflight_bridge_down_not_fatal(tmp_path, monkeypatch):
    monkeypatch.setattr(pf.settings, "document_storage_dir", str(tmp_path / "d"))
    monkeypatch.setattr(pf.settings, "bridge_token", "TA-secret")

    async def _down(self):
        return False

    _install_fake_bridge_available(monkeypatch, _down)
    # Must return (not raise) even though the bridge is down.
    res = await pf.run_preflight(check_bridge=True)
    assert res.documents_dir_writable is True
    assert res.bridge_token_set is True
    assert res.bridge_reachable is False


@pytest.mark.asyncio
async def test_run_preflight_reports_both_problems(tmp_path, monkeypatch):
    a_file = tmp_path / "not-a-dir"
    a_file.write_bytes(b"x")
    monkeypatch.setattr(pf.settings, "document_storage_dir", str(a_file / "sub"))
    monkeypatch.setattr(pf.settings, "bridge_token", "")
    # Bridge skipped — exercises the check_bridge=False branch.
    res = await pf.run_preflight(check_bridge=False)
    assert res.documents_dir_writable is False
    assert res.bridge_token_set is False
    assert res.bridge_reachable is False
    assert len(res.details) == 2
