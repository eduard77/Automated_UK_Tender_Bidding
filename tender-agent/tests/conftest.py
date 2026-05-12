"""Shared test helpers.

The adapter tests share a pattern: build a MockTransport that returns a fixture
payload, wire it into an httpx.AsyncClient, hand the client to the adapter, and
collect the yielded NormalisedTender objects. The helpers below avoid duplicating
that boilerplate across five test files.

All adapter tests are 100% offline by construction — the transport only ever
returns the fixture, no real HTTP is performed.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from datetime import datetime
from pathlib import Path

import httpx

from tender_agent.adapters.base import SourceAdapter
from tender_agent.schemas import NormalisedTender

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_json_fixture(name: str) -> dict:
    """Load a JSON fixture by filename (relative to tests/fixtures/)."""
    with (FIXTURES_DIR / name).open(encoding="utf-8") as f:
        return json.load(f)


def load_text_fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def static_json_handler(
    payload: dict, *, captured: list[httpx.Request] | None = None
) -> Callable[[httpx.Request], httpx.Response]:
    """Return a MockTransport handler that always responds with `payload`.

    If `captured` is provided, each incoming request is appended to it. Tests
    use this to assert what params the adapter sent upstream (e.g. updatedFrom).
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured.append(request)
        return httpx.Response(200, json=payload)

    return handler


def static_text_handler(
    text: str,
    *,
    content_type: str = "application/atom+xml",
    captured: list[httpx.Request] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured.append(request)
        return httpx.Response(
            200, content=text.encode("utf-8"), headers={"Content-Type": content_type}
        )

    return handler


def build_adapter(
    adapter_cls: type[SourceAdapter],
    handler: Callable[[httpx.Request], httpx.Response],
) -> SourceAdapter:
    """Instantiate `adapter_cls` with a MockTransport-backed AsyncClient."""
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return adapter_cls(client=client)


async def collect(
    adapter: SourceAdapter, since: datetime
) -> list[NormalisedTender]:
    """Collect everything an adapter yields for `since`. Closes the adapter
    afterwards so the underlying client is released."""
    out: list[NormalisedTender] = []
    iterator: AsyncIterator[NormalisedTender] = adapter.fetch_since(since)
    try:
        async for tender in iterator:
            out.append(tender)
    finally:
        await adapter.aclose()
    return out
