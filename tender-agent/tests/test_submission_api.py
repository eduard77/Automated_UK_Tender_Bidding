"""End-to-end API tests for the submission-package endpoints.

Uses an in-memory SQLite via the same fixture chunk 6 + go-no-go use, and
overrides `get_drafting_agent` to inject a fake LLM client. ZERO real
network.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tender_agent.api import submission as submission_api
from tender_agent.db import get_db
from tender_agent.main import app
from tender_agent.services.submission.engine import DraftingAgent
from tests._submission_fixtures import (
    FakeDraftingLLMClient,
    fresh_engine,
    make_complete_brief,
    make_tender,
    populate_typical_vault,
    static_response,
    technical_capability_payload,
)


@pytest.fixture()
def db_factory():
    _, factory = fresh_engine()
    return factory


@pytest.fixture()
def db(db_factory):
    s = db_factory()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def client_with_fake_agent(db_factory):
    """TestClient with both get_db and get_drafting_agent overridden."""

    def _db_override():
        s = db_factory()
        try:
            yield s
        finally:
            s.close()

    # Default fake — happy-path technical capability. Tests that need a
    # different fake call `_install_fake` below to swap the agent.
    state: dict = {"agent": None}

    def _agent_override() -> DraftingAgent:
        if state["agent"] is None:
            raise RuntimeError("install a fake via _install_fake first")
        return state["agent"]

    app.dependency_overrides[get_db] = _db_override
    app.dependency_overrides[submission_api.get_drafting_agent] = _agent_override
    tc = TestClient(app)

    def _install_fake(payload: dict) -> None:
        state["agent"] = DraftingAgent(FakeDraftingLLMClient(static_response(payload)))

    yield tc, _install_fake
    app.dependency_overrides.clear()


def _draft_body(template_id: str = "technical_capability") -> dict:
    return {
        "template_id": template_id,
        "question_text": "Describe your technical approach.",
        "question_weight_pct": 25.0,
        "word_limit": 800,
        "evaluation_criteria": "Marked 0-5 on methodology, evidence, team, risks.",
        "buyer_context": {
            "strategy_docs": ["Digital Strategy 2025"],
            "operational_context": "North-West sites.",
            "priorities": ["Recycling rates"],
        },
        "org_id": 1,
    }


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_post_draft_creates_package_and_draft(client_with_fake_agent, db) -> None:
    tc, install = client_with_fake_agent
    tender = make_tender(db)
    make_complete_brief(db, tender_id=tender.id)
    vault = populate_typical_vault(db)
    install(
        technical_capability_payload(
            vault_iso=vault["iso_27001"], vault_case_study=vault["case_study"]
        )
    )

    r = tc.post(
        f"/tenders/{tender.id}/submission-package/questions",
        json=_draft_body(),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["template_id"] == "technical_capability"
    assert body["schema_version"] == "1.6"
    assert body["status"] == "needs_review"
    assert body["validation_report"] is not None


def test_get_package_returns_drafts(client_with_fake_agent, db) -> None:
    tc, install = client_with_fake_agent
    tender = make_tender(db)
    make_complete_brief(db, tender_id=tender.id)
    vault = populate_typical_vault(db)
    install(
        technical_capability_payload(
            vault_iso=vault["iso_27001"], vault_case_study=vault["case_study"]
        )
    )

    tc.post(f"/tenders/{tender.id}/submission-package/questions", json=_draft_body())
    r = tc.get(f"/tenders/{tender.id}/submission-package")
    assert r.status_code == 200
    body = r.json()
    assert body is not None
    assert body["tender_id"] == tender.id
    assert len(body["drafts"]) == 1


def test_get_single_draft_returns_full_payload(client_with_fake_agent, db) -> None:
    tc, install = client_with_fake_agent
    tender = make_tender(db)
    make_complete_brief(db, tender_id=tender.id)
    vault = populate_typical_vault(db)
    install(
        technical_capability_payload(
            vault_iso=vault["iso_27001"], vault_case_study=vault["case_study"]
        )
    )
    posted = tc.post(
        f"/tenders/{tender.id}/submission-package/questions", json=_draft_body()
    )
    draft_id = posted.json()["id"]
    r = tc.get(
        f"/tenders/{tender.id}/submission-package/questions/{draft_id}"
    )
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == draft_id
    assert body["structured_content"]["methodology"]["methodology_name"]


# ---------------------------------------------------------------------------
# Error states
# ---------------------------------------------------------------------------


def test_unknown_tender_404(client_with_fake_agent, db) -> None:
    tc, install = client_with_fake_agent
    install({})  # never used; the 404 fires first.
    r = tc.post(
        "/tenders/999999/submission-package/questions", json=_draft_body()
    )
    assert r.status_code == 404


def test_brief_not_ready_409(client_with_fake_agent, db) -> None:
    tc, install = client_with_fake_agent
    tender = make_tender(db)
    # No brief.
    install({})
    r = tc.post(
        f"/tenders/{tender.id}/submission-package/questions",
        json=_draft_body(),
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "brief_not_ready"


def test_template_out_of_scope_400(client_with_fake_agent, db) -> None:
    tc, install = client_with_fake_agent
    tender = make_tender(db)
    make_complete_brief(db, tender_id=tender.id)
    install({})
    body = _draft_body()
    body["template_id"] = "pricing_schedule"
    r = tc.post(
        f"/tenders/{tender.id}/submission-package/questions",
        json=body,
    )
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "template_out_of_scope"


def test_get_package_returns_null_when_none(client_with_fake_agent, db) -> None:
    tc, _ = client_with_fake_agent
    tender = make_tender(db)
    make_complete_brief(db, tender_id=tender.id)
    r = tc.get(f"/tenders/{tender.id}/submission-package")
    assert r.status_code == 200
    assert r.json() is None


# ---------------------------------------------------------------------------
# No-submit invariant — the routes carry no submit verb
# ---------------------------------------------------------------------------


def _iter_route_paths(routes) -> list[str]:
    """Every leaf route path on the app, recursing into included routers.

    Starlette >= 1.3 / FastAPI >= 0.137 stopped FLATTENING an
    ``include_router()``'d router into ``app.routes``: the router now appears
    as a single opaque wrapper object (``_IncludedRouter``) instead of its
    child ``APIRoute``s being spliced directly into ``app.routes``. The
    wrapper exposes neither ``.path`` nor ``.routes`` — only
    ``.original_router`` (whose ``.routes`` carry the real, prefixed paths).
    A flat scan of ``app.routes`` therefore finds NONE of the submission
    paths on the new version — a false negative that has nothing to do with
    whether a submit endpoint exists (the submission router still carries
    exactly its draft+read routes). Recurse through ``.routes`` (old
    flattening / Mounts) AND ``.original_router.routes`` (the new wrapper) so
    the assertion holds on both representations."""
    paths: list[str] = []
    for route in routes:
        path = getattr(route, "path", None)
        if isinstance(path, str) and path:
            paths.append(path)
        nested = getattr(route, "routes", None)
        if nested is None:
            original = getattr(route, "original_router", None)
            nested = getattr(original, "routes", None) if original is not None else None
        if nested:
            paths.extend(_iter_route_paths(nested))
    return paths


def test_no_submit_endpoint_exists() -> None:
    """The submission router exposes only draft + read paths. There is
    no endpoint that submits, confirms, or sends — that's a portal-level
    action behind a different, human-gated rail."""
    submission_paths = [
        p for p in _iter_route_paths(app.routes) if "submission-package" in p
    ]
    assert submission_paths, "expected submission routes registered"
    # No submit/confirm/send verb anywhere on the submission rail.
    for path in submission_paths:
        assert not path.endswith("/submit")
        assert "/confirm" not in path
        assert "/send" not in path
