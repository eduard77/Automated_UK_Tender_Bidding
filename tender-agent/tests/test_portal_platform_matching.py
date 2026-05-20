"""Every seeded platform's domain_patterns must match >= 3 sample URLs from
the live database — the guarantee that the platform model actually clusters
real portals.

DB-backed: uses the same Postgres as the rest of the suite (seeded by
migration 0006).
"""
from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from tender_agent.db import engine
from tender_agent.models import Portal, PortalPlatform, PortalUrlSighting
from tender_agent.services.platform_matching import (
    domain_matches,
    find_platform_for_domain,
)


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


def test_ten_platforms_seeded(db: Session) -> None:
    # 10 data-derived platforms from chunk 2, plus contracts_finder_direct
    # (chunk 3) which is config-seeded for the adapter.
    count = len(db.execute(select(PortalPlatform)).scalars().all())
    assert count >= 10


def test_every_platform_matches_at_least_three_sightings(db: Session) -> None:
    # This guarantee is about the *live* dataset. CI runs against an empty
    # portals table (migrations only, no backfill), so skip there rather than
    # fail — the dev-DB smoke run in the PR proves the real assertion.
    portal_total = len(db.execute(select(Portal)).scalars().all())
    if portal_total == 0:
        pytest.skip("no portal data in DB (CI) — verified in dev-DB smoke run")
    platforms = db.execute(select(PortalPlatform)).scalars().all()
    assert platforms, "no platforms seeded"
    for platform in platforms:
        # contracts_finder_direct (chunk 3) is config-seeded for the adapter;
        # its assets.publishing portals only appear once such a doc is
        # ingested, so it's exempt from the "must match live traffic" rule.
        if platform.slug == "contracts_finder_direct":
            continue
        # Portals whose domain matches this platform's patterns.
        portals = db.execute(select(Portal)).scalars().all()
        matched_ids = [
            p.id
            for p in portals
            if domain_matches(p.domain, platform.domain_patterns or [])
        ]
        assert matched_ids, f"{platform.slug} matched no portals"
        sighting_count = db.execute(
            select(PortalUrlSighting).where(
                PortalUrlSighting.portal_id.in_(matched_ids)
            )
        ).scalars().all()
        assert len(sighting_count) >= 3, (
            f"{platform.slug} matched only {len(sighting_count)} sightings"
        )


def test_delta_links_known_subdomains(db: Session) -> None:
    delta = db.execute(
        select(PortalPlatform).where(PortalPlatform.slug == "delta_esourcing")
    ).scalar_one()
    for domain in (
        "delta-esourcing.com",
        "teesvalley.delta-esourcing.com",
        "neupc.delta-esourcing.com",
        "londonbarnet.delta-esourcing.com",
    ):
        assert domain_matches(domain, delta.domain_patterns)


def test_email_and_govuk_domains_match_no_platform(db: Session) -> None:
    assert find_platform_for_domain(db, "nhs.net") is None
    assert find_platform_for_domain(db, "justice.gov.uk") is None
    assert find_platform_for_domain(db, "buckscc.gov.uk") is None


def test_find_platform_for_known_domain(db: Session) -> None:
    platform = find_platform_for_domain(db, "procontract.due-north.com")
    assert platform is not None
    assert platform.slug == "procontract"
