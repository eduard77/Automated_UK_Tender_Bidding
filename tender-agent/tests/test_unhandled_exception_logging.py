"""The global unhandled-exception handler must log a full traceback.

This pins the *visibility* half of the matched_only outage: the 500 produced no
traceback in the app logs (nothing logged the exception), so the cause was
invisible in `az webapp log tail`. We now register a catch-all handler that logs
the traceback as a structured field. This test calls that handler directly with a
live exception context and asserts the traceback is recorded.
"""
from __future__ import annotations

import asyncio

from starlette.requests import Request
from structlog.testing import capture_logs

from tender_agent.main import unhandled_exception_handler


def _fake_request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/tenders",
            "query_string": b"matched_only=true",
            "headers": [],
        }
    )


def test_unhandled_exception_is_logged_with_traceback() -> None:
    request = _fake_request()
    try:
        raise ValueError("boom from the matched_only path")
    except ValueError as exc:
        with capture_logs() as logs:
            resp = asyncio.run(unhandled_exception_handler(request, exc))

    # Generic 500 to the client; internals are logged, never leaked in the body.
    assert resp.status_code == 500
    assert b"boom" not in resp.body

    events = [e for e in logs if e.get("event") == "unhandled_exception"]
    assert len(events) == 1
    event = events[0]
    assert event["error_type"] == "ValueError"
    assert event["path"] == "/tenders"
    assert event["query"] == "matched_only=true"
    # The whole point: a real, multi-line traceback is captured.
    assert "Traceback (most recent call last)" in event["traceback"]
    assert "ValueError: boom from the matched_only path" in event["traceback"]
