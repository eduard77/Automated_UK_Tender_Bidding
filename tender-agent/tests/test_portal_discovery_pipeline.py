"""End-to-end test of the portal discovery pipeline using a real DB session.

These tests require the same Postgres that powers the rest of the test suite
(see ci.yml). Each test runs inside a SAVEPOINT that's rolled back at the
end, so they're hermetic.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from tender_agent.db import engine
from tender_agent.models import (
    Portal,
    PortalBlocklistDomain,
    PortalUrlSighting,
    Tender,
)
from tender_agent.services.portal_discovery import (
    extract_urls_from_tender,
    process_tender_for_portals,
)


@pytest.fixture()
def db() -> Session:
    """A Session joined to an outer transaction with a SAVEPOINT — service
    calls can commit freely; the whole tree is rolled back on teardown.
    See SQLAlchemy "Joining a Session into an External Transaction"."""
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


def _make_tender(
    db: Session,
    *,
    description: str | None = None,
    documents: list | None = None,
    raw: dict | None = None,
    source_ref: str = "test-1",
) -> Tender:
    now = datetime.now(UTC)
    tender = Tender(
        source_code="TEST",
        source_ref=source_ref,
        title="Test tender",
        description=description,
        documents=documents,
        raw=raw,
        first_seen_at=now,
        last_seen_at=now,
    )
    db.add(tender)
    db.flush()
    return tender


def test_extract_urls_from_description_and_documents(db: Session) -> None:
    tender = _make_tender(
        db,
        description=(
            "Submissions via https://procontract.due-north.com/Tenders/View/12345 "
            "or contact procurement@buyer.local."
        ),
        documents=[
            {"title": "ITT", "url": "https://procontract.due-north.com/Documents/abc"},
            {"title": "Spec", "url": "https://files.example-portal.com/spec.pdf"},
        ],
        source_ref="extract-1",
    )

    extracted = extract_urls_from_tender(tender, db)
    domains = {e.domain for e in extracted}
    assert "procontract.due-north.com" in domains
    assert "files.example-portal.com" in domains
    # email -> contact_email sighting on the same buyer.local domain
    assert "buyer.local" in domains
    kinds = {(e.domain, e.sighting_type) for e in extracted}
    assert ("buyer.local", "contact_email") in kinds


def test_blocklist_filters_source_domains(db: Session) -> None:
    tender = _make_tender(
        db,
        description=(
            "See https://www.contractsfinder.service.gov.uk/Notice/abc "
            "and https://portal.example-buyer.com/tender/1"
        ),
        source_ref="extract-block-1",
    )
    extracted = extract_urls_from_tender(tender, db)
    domains = {e.domain for e in extracted}
    assert "contractsfinder.service.gov.uk" not in domains
    assert "portal.example-buyer.com" in domains


def test_gov_uk_apex_blocked_but_subdomain_allowed(db: Session) -> None:
    tender = _make_tender(
        db,
        description=(
            "Background at https://www.gov.uk/government/publications/foo "
            "and registration at https://nepo.gov.uk/registration"
        ),
        source_ref="extract-block-gov",
    )
    extracted = extract_urls_from_tender(tender, db)
    domains = {e.domain for e in extracted}
    assert "gov.uk" not in domains
    assert "nepo.gov.uk" in domains


def test_process_tender_creates_portal_and_sighting(db: Session) -> None:
    tender = _make_tender(
        db,
        description="Via https://newportal.example.com/tender/9",
        source_ref="process-new-1",
    )
    result = process_tender_for_portals(tender, db)
    assert result.new_portals == 1
    assert result.new_sightings == 1
    assert result.classifications_queued == 1

    portal = db.execute(
        select(Portal).where(Portal.domain == "newportal.example.com")
    ).scalar_one()
    assert portal.tender_count == 1
    assert portal.display_name == "newportal.example.com"
    sighting = db.execute(
        select(PortalUrlSighting).where(PortalUrlSighting.portal_id == portal.id)
    ).scalar_one()
    assert sighting.tender_id == tender.id


def test_process_tender_is_idempotent(db: Session) -> None:
    tender = _make_tender(
        db,
        description="https://idemportal.example.com/tender/1",
        source_ref="process-idem-1",
    )
    first = process_tender_for_portals(tender, db)
    second = process_tender_for_portals(tender, db)
    assert first.new_portals == 1
    assert first.new_sightings == 1
    assert second.new_portals == 0
    assert second.new_sightings == 0
    portal = db.execute(
        select(Portal).where(Portal.domain == "idemportal.example.com")
    ).scalar_one()
    # tender_count must NOT double-count when we re-process the same tender.
    assert portal.tender_count == 1


def test_two_tenders_on_one_portal_bumps_tender_count(db: Session) -> None:
    t1 = _make_tender(
        db,
        description="https://shared.example.com/tender/1",
        source_ref="shared-1",
    )
    t2 = _make_tender(
        db,
        description="https://shared.example.com/tender/2",
        source_ref="shared-2",
    )
    process_tender_for_portals(t1, db)
    process_tender_for_portals(t2, db)
    portal = db.execute(
        select(Portal).where(Portal.domain == "shared.example.com")
    ).scalar_one()
    assert portal.tender_count == 2


def test_extraction_handles_party_contactpoint(db: Session) -> None:
    tender = _make_tender(
        db,
        raw={
            "releases": [
                {
                    "parties": [
                        {
                            "contactPoint": {
                                "url": "https://contact.example.com/buyer",
                                "email": "ops@buyer.example",
                            }
                        }
                    ]
                }
            ]
        },
        source_ref="parties-1",
    )
    extracted = extract_urls_from_tender(tender, db)
    pairs = {(e.domain, e.extracted_from, e.sighting_type) for e in extracted}
    assert ("contact.example.com", "parties", "tender_link") in pairs
    assert ("buyer.example", "parties", "contact_email") in pairs


def test_user_added_blocklist_entry_filters(db: Session) -> None:
    db.add(
        PortalBlocklistDomain(
            domain="customblock.example.com",
            reason="user test",
            added_by="user",
        )
    )
    db.flush()
    tender = _make_tender(
        db,
        description=(
            "https://customblock.example.com/page "
            "and https://goodportal.example.com/tender"
        ),
        source_ref="userblock-1",
    )
    extracted = extract_urls_from_tender(tender, db)
    domains = {e.domain for e in extracted}
    assert "customblock.example.com" not in domains
    assert "goodportal.example.com" in domains
