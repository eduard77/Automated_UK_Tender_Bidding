"""Tests for ingestion status reporting.

Regression test: when an adapter's upstream HTTP requests all fail, the
adapter would yield no records and the previous version of poll_source would
log status="ok" — masking the failure. The adapter now sets
`had_errors=True` and ingestion translates that into status="error" without
advancing `source.last_polled_at`.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from tender_agent.adapters.base import SourceAdapter
from tender_agent.schemas import NormalisedTender
from tender_agent.services.ingestion import poll_source


class _NoYieldFailingAdapter(SourceAdapter):
    """Simulates an adapter whose upstream calls all fail before yielding anything."""

    code = "FAKE"
    name = "Fake adapter"
    base_url = "https://example.invalid"

    async def fetch_since(
        self, since: datetime
    ) -> AsyncIterator[NormalisedTender]:
        self.had_errors = True
        if False:  # pragma: no cover
            yield  # type: ignore[unreachable]


class _CleanEmptyAdapter(SourceAdapter):
    """Simulates an adapter whose upstream returned an empty result set cleanly."""

    code = "FAKE"
    name = "Fake adapter"
    base_url = "https://example.invalid"

    async def fetch_since(
        self, since: datetime
    ) -> AsyncIterator[NormalisedTender]:
        if False:  # pragma: no cover
            yield  # type: ignore[unreachable]


class _SpecificErrorAdapter(SourceAdapter):
    """Simulates S2W/NI: upstream fails and the adapter records the SPECIFIC
    cause (status / exception) via record_error, not just had_errors."""

    code = "FAKE"
    name = "Fake adapter"
    base_url = "https://example.invalid"

    async def fetch_since(
        self, since: datetime
    ) -> AsyncIterator[NormalisedTender]:
        self.had_errors = True
        self.record_error(
            "06-2026: HTTPStatusError: 500 Server Error for url .../v1/Notices"
        )
        if False:  # pragma: no cover
            yield  # type: ignore[unreachable]


def _fake_source() -> MagicMock:
    source = MagicMock()
    source.id = 42
    source.code = "FAKE"
    source.last_polled_at = None
    return source


@pytest.mark.asyncio
async def test_poll_source_marks_status_error_when_adapter_had_errors() -> None:
    source = _fake_source()
    db = MagicMock()

    with patch.dict(
        "tender_agent.services.ingestion.ADAPTERS",
        {"FAKE": _NoYieldFailingAdapter},
        clear=False,
    ):
        run = await poll_source(db, source)

    assert run.status == "error"
    assert run.fetched == 0
    assert run.error is not None
    # last_polled_at must NOT be advanced when the source failed.
    assert source.last_polled_at is None


@pytest.mark.asyncio
async def test_poll_source_surfaces_specific_adapter_error_not_generic() -> None:
    """When an adapter records a SPECIFIC upstream cause via record_error
    (the S2W/NI path), poll_source must surface that on PollRun.error —
    NOT the legacy generic "upstream HTTP requests failed" string. This is
    what lets the sources-health readout tell 403 vs 500 vs DNS apart."""
    source = _fake_source()
    db = MagicMock()

    with patch.dict(
        "tender_agent.services.ingestion.ADAPTERS",
        {"FAKE": _SpecificErrorAdapter},
        clear=False,
    ):
        run = await poll_source(db, source)

    assert run.status == "error"
    assert "HTTPStatusError" in (run.error or "")
    assert "500" in (run.error or "")
    assert "upstream HTTP requests failed" not in (run.error or "")
    # A failed source must NOT advance its watermark.
    assert source.last_polled_at is None


@pytest.mark.asyncio
async def test_poll_source_marks_status_ok_when_adapter_returned_empty() -> None:
    source = _fake_source()
    db = MagicMock()

    pre_poll = datetime.now(UTC)
    with patch.dict(
        "tender_agent.services.ingestion.ADAPTERS",
        {"FAKE": _CleanEmptyAdapter},
        clear=False,
    ):
        run = await poll_source(db, source)

    assert run.status == "ok"
    assert run.fetched == 0
    assert run.error is None
    # A clean run advances last_polled_at on the source.
    assert source.last_polled_at is not None
    assert source.last_polled_at >= pre_poll
