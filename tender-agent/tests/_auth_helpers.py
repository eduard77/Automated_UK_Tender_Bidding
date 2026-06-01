"""Test helper: authenticate a TestClient as a fully-entitled account.

Chunk 6 introduced server-side gating on the brief + document endpoints
(401 anonymous, 402 unentitled). Pre-existing tests that asserted the
unauthenticated behaviour now need to be lifted past the gate to exercise
their original assertions.

This helper:
  1. signs up a fresh test account (random email so repeats don't collide),
  2. uses the dev-only override to set the account's plan to plan_unlimited
     so it is entitled for EVERY tender without needing per-tender rows.

The resulting TestClient carries the auth cookie via TestClient's cookie jar.
The dev override is REFUSED in production — these helpers are test-only.
"""
from __future__ import annotations

import uuid

from fastapi.testclient import TestClient


def authenticate_unlimited(client: TestClient) -> dict:
    """Sign up and elevate to plan_unlimited. Returns the /me payload."""
    email = f"test-{uuid.uuid4().hex[:12]}@example.com"
    password = "TestPassword123!"
    r = client.post(
        "/accounts/signup", json={"email": email, "password": password}
    )
    assert r.status_code == 200, r.text
    me = r.json()
    r = client.post(
        "/accounts/dev/override",
        json={"account_id": me["id"], "plan": "plan_unlimited"},
    )
    assert r.status_code == 200, r.text
    return r.json()


def authenticate_and_entitle_tender(
    client: TestClient, tender_id: int
) -> dict:
    """Sign up and grant a per-tender brief_entitlement (source='dev').

    Use this when a test wants to verify per-tender entitlement behaviour;
    use `authenticate_unlimited` otherwise — it's the cheaper default for
    tests that just need to be past the gate.
    """
    email = f"test-{uuid.uuid4().hex[:12]}@example.com"
    password = "TestPassword123!"
    r = client.post(
        "/accounts/signup", json={"email": email, "password": password}
    )
    assert r.status_code == 200, r.text
    me = r.json()
    r = client.post(
        "/accounts/dev/override",
        json={"account_id": me["id"], "grant_tender_id": tender_id},
    )
    assert r.status_code == 200, r.text
    return r.json()
