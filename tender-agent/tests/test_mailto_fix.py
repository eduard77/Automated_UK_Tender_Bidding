"""mailto: extraction creates contact_email sightings that do NOT inflate
tender_count, and flips is_email_domain correctly.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from tender_agent.db import engine
from tender_agent.models import Portal, PortalUrlSighting, Tender
from tender_agent.services.portal_discovery import process_tender_for_portals


@pytest.fixture()
def db() -> Session:
    connection = engine.connect()
    outer = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        outer.rollback()
        connection.close()


def _tender(db: Session, *, description: str, ref: str) -> Tender:
    now = datetime.now(UTC)
    t = Tender(
        source_code="TEST",
        source_ref=ref,
        title="t",
        description=description,
        first_seen_at=now,
        last_seen_at=now,
    )
    db.add(t)
    db.flush()
    return t


def _portal(db: Session, domain: str) -> Portal:
    return db.execute(
        select(Portal).where(Portal.domain == domain)
    ).scalar_one()


def test_email_only_portal_flagged_and_zero_count(db: Session) -> None:
    t = _tender(
        db,
        description="Contact procurement@buyer-mail.example for details.",
        ref="mailto-only-1",
    )
    process_tender_for_portals(t, db)
    portal = _portal(db, "buyer-mail.example")
    assert portal.is_email_domain is True
    assert portal.tender_count == 0
    sighting = db.execute(
        select(PortalUrlSighting).where(PortalUrlSighting.portal_id == portal.id)
    ).scalar_one()
    assert sighting.sighting_type == "contact_email"


def test_link_domain_counts_email_domain_does_not(db: Session) -> None:
    t = _tender(
        db,
        description=(
            "Submit via https://realportal.example/tender/1 or email "
            "team@mailer.example."
        ),
        ref="mixed-1",
    )
    process_tender_for_portals(t, db)
    link_portal = _portal(db, "realportal.example")
    email_portal = _portal(db, "mailer.example")
    assert link_portal.tender_count == 1
    assert link_portal.is_email_domain is False
    assert email_portal.tender_count == 0
    assert email_portal.is_email_domain is True


def test_email_then_link_flips_flag(db: Session) -> None:
    # First sighting: email only.
    t1 = _tender(
        db,
        description="Email flip@flipper.example for the pack.",
        ref="flip-1",
    )
    process_tender_for_portals(t1, db)
    portal = _portal(db, "flipper.example")
    assert portal.is_email_domain is True
    assert portal.tender_count == 0

    # Second tender: a real link on the same domain.
    t2 = _tender(
        db,
        description="Now live at https://flipper.example/notice/2",
        ref="flip-2",
    )
    process_tender_for_portals(t2, db)
    db.refresh(portal)
    assert portal.is_email_domain is False
    assert portal.tender_count == 1
