"""Tests for the Claude-powered requirements extractor."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from tender_agent.services.requirements_extractor import _strip_fences, _build_prompt


def test_strip_fences_handles_plain_json():
    raw = '{"summary": "ok"}'
    assert _strip_fences(raw) == raw


def test_strip_fences_handles_markdown_fence():
    raw = '```json\n{"summary": "ok"}\n```'
    assert _strip_fences(raw) == '{"summary": "ok"}'


def test_strip_fences_handles_bare_fence():
    raw = '```\n{"a": 1}\n```'
    assert _strip_fences(raw) == '{"a": 1}'


def test_build_prompt_includes_metadata(monkeypatch):
    from datetime import datetime, timezone
    from decimal import Decimal

    from tender_agent.models import Tender

    t = Tender(
        source_code="FTS",
        source_ref="ocid-1",
        source_url="https://example.gov.uk/n/1",
        title="Cleaning",
        buyer_name="Bristol CC",
        value_amount=Decimal("100000"),
        value_currency="GBP",
        deadline_at=datetime(2025, 12, 1, tzinfo=timezone.utc),
        cpv_codes=["909"],
    )
    prompt = _build_prompt(t, "DOC TEXT HERE")
    assert "Cleaning" in prompt
    assert "Bristol CC" in prompt
    assert "GBP 100000" in prompt
    assert "909" in prompt
    assert "DOC TEXT HERE" in prompt
    assert "2025-12-01" in prompt
