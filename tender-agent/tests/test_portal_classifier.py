"""Classifier tests. Real Claude is never called — we monkeypatch the
`classifier_backend` hook + `_fetch_homepage` to return canned responses.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from tender_agent.db import engine
from tender_agent.models import Portal
from tender_agent.services import portal_classifier
from tender_agent.services.portal_classifier import classify_portal


@pytest.fixture()
def db() -> Session:
    connection = engine.connect()
    outer = connection.begin()
    session = Session(
        bind=connection, join_transaction_mode="create_savepoint"
    )
    try:
        yield session
    finally:
        session.close()
        outer.rollback()
        connection.close()


def _make_portal(db: Session, *, domain: str) -> Portal:
    now = datetime.now(UTC)
    portal = Portal(
        domain=domain,
        display_name=domain,
        first_seen_at=now,
        last_seen_at=now,
        created_at=now,
        updated_at=now,
    )
    db.add(portal)
    db.flush()
    return portal


def test_classifier_high_confidence_overrides_defaults(
    monkeypatch: pytest.MonkeyPatch, db: Session
) -> None:
    portal = _make_portal(db, domain="hi-confidence.example.com")
    monkeypatch.setattr(
        portal_classifier, "_fetch_homepage", lambda *a, **kw: "<html>...</html>"
    )
    monkeypatch.setattr(
        portal_classifier,
        "classifier_backend",
        lambda domain, sample, html: {
            "is_procurement_portal": True,
            "confidence": 0.92,
            "suggested_display_name": "High Confidence Portal",
            "login_type": "username_password",
            "suggested_priority": "high",
            "reasoning": "Looks like a buyer-hosted portal.",
            "notes": "",
        },
    )
    result = classify_portal(portal.id, db)
    assert result.status == "classified"
    db.refresh(portal)
    assert portal.display_name == "High Confidence Portal"
    assert portal.login_type == "username_password"
    assert portal.priority == "high"
    assert portal.classification_data is not None
    assert portal.classification_data.get("is_procurement_portal") is True


def test_classifier_low_confidence_keeps_defaults(
    monkeypatch: pytest.MonkeyPatch, db: Session
) -> None:
    portal = _make_portal(db, domain="low-confidence.example.com")
    monkeypatch.setattr(
        portal_classifier, "_fetch_homepage", lambda *a, **kw: "<html>...</html>"
    )
    monkeypatch.setattr(
        portal_classifier,
        "classifier_backend",
        lambda domain, sample, html: {
            "is_procurement_portal": False,
            "confidence": 0.4,
            "suggested_display_name": "Maybe a Portal",
            "login_type": "username_password",
            "suggested_priority": "high",
            "reasoning": "Couldn't tell from the HTML",
            "notes": "",
        },
    )
    classify_portal(portal.id, db)
    db.refresh(portal)
    # Defaults preserved because confidence < 0.7
    assert portal.display_name == "low-confidence.example.com"
    assert portal.login_type == "unknown"
    assert portal.priority == "medium"


def test_classifier_handles_fetch_failure(
    monkeypatch: pytest.MonkeyPatch, db: Session
) -> None:
    portal = _make_portal(db, domain="dead.example.com")
    monkeypatch.setattr(portal_classifier, "_fetch_homepage", lambda *a, **kw: None)
    called = {"n": 0}

    def _should_not_be_called(*_, **__):  # pragma: no cover
        called["n"] += 1
        return

    monkeypatch.setattr(portal_classifier, "classifier_backend", _should_not_be_called)
    result = classify_portal(portal.id, db)
    assert result.status == "fetch_failed"
    assert called["n"] == 0
    db.refresh(portal)
    assert portal.classification_data == {
        "_status": "fetch_failed",
        "_classified_at": portal.classification_data["_classified_at"],
    }


def test_classifier_handles_claude_unavailable(
    monkeypatch: pytest.MonkeyPatch, db: Session
) -> None:
    portal = _make_portal(db, domain="claudedown.example.com")
    monkeypatch.setattr(
        portal_classifier, "_fetch_homepage", lambda *a, **kw: "<html>ok</html>"
    )
    monkeypatch.setattr(
        portal_classifier, "classifier_backend", lambda *a, **kw: None
    )
    result = classify_portal(portal.id, db)
    assert result.status == "claude_unavailable"
    db.refresh(portal)
    assert portal.classification_data["_status"] == "claude_unavailable"
