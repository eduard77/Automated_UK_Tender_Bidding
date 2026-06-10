"""Test FilterProfile seeding.

A profile-driven Proactis discovery run needs at least one enabled FilterProfile
to translate into a Proactis filter config. On a fresh cloud DB `GET /filters`
returns [] — this seeder creates ONE deliberately broad construction-sector
profile so the operator can exercise the end-to-end path (store-login → trigger
run-for-profile → see rows land) without hand-crafting JSON.

The values are intentionally permissive: CPV prefixes covering the main UK
construction families, no region/keyword/value narrowing. That maximises the
chance the test run returns rows and that the Dynatree popup actually has
matching nodes to tick.

Idempotent by `name` — a second call returns the existing row, never duplicates.
The shared `name` constant is what makes the seed safe to run from both the
admin endpoint and the script without coordination.
"""
from __future__ import annotations

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from tender_agent.models import FilterProfile

logger = structlog.get_logger(__name__)

TEST_PROFILE_NAME = "Test - UK Construction"

# Two-digit CPV prefixes covering the main UK construction-sector families. We
# keep them as PREFIXES (not specific codes) so the profile matches broadly
# across construction work, professional services, repair/maintenance, materials
# and utilities — the same rationale §5.6 of PROJECT.md gives for prefix-based
# CPV matching.
TEST_PROFILE_CPV_PREFIXES: tuple[str, ...] = (
    "45",  # Construction work
    "71",  # Architectural, construction, engineering and inspection services
    "50",  # Repair and maintenance services
    "44",  # Construction structures and materials; auxiliary products
    "09",  # Petroleum products, fuel, electricity and other energy sources
)


def ensure_test_filter_profile(db: Session) -> tuple[FilterProfile, bool]:
    """Get-or-create the canonical test profile. Returns (profile, created).

    Lookup is by NAME — we never duplicate the seed row, even if the operator
    has tweaked CPV prefixes or other fields by hand since the first run."""
    existing = db.execute(
        select(FilterProfile).where(FilterProfile.name == TEST_PROFILE_NAME)
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False

    profile = FilterProfile(
        name=TEST_PROFILE_NAME,
        enabled=True,
        cpv_prefixes=list(TEST_PROFILE_CPV_PREFIXES),
    )
    db.add(profile)
    db.commit()
    db.refresh(profile)
    logger.info(
        "filters.test_profile_seeded",
        profile_id=profile.id,
        cpv_prefixes=list(TEST_PROFILE_CPV_PREFIXES),
    )
    return profile, True
