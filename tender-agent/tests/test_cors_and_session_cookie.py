"""Cross-origin plumbing for the deployed dashboard — CORS + session cookie.

The deployed dashboard (genera-tenders-dashboard…azurewebsites.net) calls the
API on a DIFFERENT origin, so two things must hold or the search page shows
no data / login silently fails:

  1. CORS: the dashboard origin must be allowed WITH credentials. Azure
     assigns the app a random-suffixed default hostname, so an explicit list
     can't name it ahead of time — `cors_allow_origin_regex` (scoped to our
     app-name prefix) covers it, and Starlette echoes the specific Origin
     back, never "*".
  2. Session cookie: browsers only send cookies on cross-site XHR when
     SameSite=None + Secure. Production auto-selects exactly that; local dev
     stays Lax (the /__api rewrite keeps dev same-origin).

All offline — TestClient preflights and a raw Response for cookie attrs.
"""
from __future__ import annotations

import pytest
from fastapi import Response
from fastapi.testclient import TestClient

from tender_agent.api.accounts import _session_cookie_attrs, _set_session_cookie
from tender_agent.config import settings
from tender_agent.main import app

DASHBOARD_ORIGINS = [
    # Unsuffixed default-hostname form.
    "https://genera-tenders-dashboard.azurewebsites.net",
    # Random-suffix + region form Azure actually assigns nowadays (mirrors
    # the backend's own generatender-gqbgaye9fmdfc4c6.ukwest-01… shape).
    "https://genera-tenders-dashboard-gqbgaye9fmdfc4c6.ukwest-01.azurewebsites.net",
]


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


# --- CORS ---------------------------------------------------------------------


@pytest.mark.parametrize("origin", DASHBOARD_ORIGINS)
def test_preflight_allows_dashboard_origin_with_credentials(
    client: TestClient, origin: str
) -> None:
    resp = client.options(
        "/tenders/facets",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
        },
    )
    assert resp.status_code == 200
    # The SPECIFIC origin is echoed (never "*") so credentials are allowed.
    assert resp.headers["access-control-allow-origin"] == origin
    assert resp.headers["access-control-allow-credentials"] == "true"


def test_preflight_rejects_foreign_origin(client: TestClient) -> None:
    resp = client.options(
        "/tenders/facets",
        headers={
            "Origin": "https://evil.example.com",
            "Access-Control-Request-Method": "GET",
        },
    )
    # Starlette answers the preflight but does NOT grant the origin.
    assert "access-control-allow-origin" not in resp.headers


def test_preflight_rejects_lookalike_azurewebsites_origin(client: TestClient) -> None:
    """The regex is scoped to OUR app-name prefix — an arbitrary
    azurewebsites.net app must not be allowed."""
    resp = client.options(
        "/tenders/facets",
        headers={
            "Origin": "https://someone-elses-app.azurewebsites.net",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert "access-control-allow-origin" not in resp.headers


def test_localhost_dev_origin_still_allowed(client: TestClient) -> None:
    resp = client.options(
        "/tenders/facets",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert resp.headers["access-control-allow-origin"] == "http://localhost:3000"


# --- session cookie attributes --------------------------------------------------


def test_production_cookie_is_samesite_none_and_secure(monkeypatch) -> None:
    monkeypatch.setattr(settings, "tender_agent_env", "production")
    monkeypatch.setattr(settings, "session_cookie_samesite", "")
    assert _session_cookie_attrs() == ("none", True)

    response = Response()
    _set_session_cookie(response, "tok-123")
    cookie = response.headers["set-cookie"].lower()
    assert "samesite=none" in cookie
    assert "secure" in cookie
    assert "httponly" in cookie
    assert "tok-123" in response.headers["set-cookie"]


def test_dev_cookie_stays_lax_and_insecure(monkeypatch) -> None:
    monkeypatch.setattr(settings, "tender_agent_env", "")
    monkeypatch.setattr(settings, "session_cookie_samesite", "")
    assert _session_cookie_attrs() == ("lax", False)

    response = Response()
    _set_session_cookie(response, "tok-dev")
    cookie = response.headers["set-cookie"].lower()
    assert "samesite=lax" in cookie
    assert "secure" not in cookie.replace("httponly", "")


def test_explicit_samesite_none_forces_secure_even_in_dev(monkeypatch) -> None:
    """SameSite=None without Secure is rejected by browsers — the helper
    never emits that combination."""
    monkeypatch.setattr(settings, "tender_agent_env", "")
    monkeypatch.setattr(settings, "session_cookie_samesite", "none")
    assert _session_cookie_attrs() == ("none", True)


def test_explicit_override_wins_over_production_auto(monkeypatch) -> None:
    monkeypatch.setattr(settings, "tender_agent_env", "production")
    monkeypatch.setattr(settings, "session_cookie_samesite", "strict")
    assert _session_cookie_attrs() == ("strict", True)
