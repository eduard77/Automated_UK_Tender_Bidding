"""Seed the canonical "Test - UK Construction" FilterProfile.

Idempotent: a second run prints the existing profile id without inserting.
The profile is intentionally broad (construction-family CPV prefixes, no
region/keyword/value narrowing) so the end-to-end logged-in Proactis test run
returns rows and the Dynatree popup actually has matching nodes to tick.

Runs against whichever DATABASE_URL the app sees — locally that is the dev
Postgres; on the cloud the operator already has the browser-only POST
/admin/seed-test-profile path and doesn't need this script.

Usage:
    cd tender-agent
    python -m scripts.seed_test_filter_profile
"""
from __future__ import annotations

import sys

from tender_agent.db import SessionLocal
from tender_agent.services.test_profile_seed import (
    TEST_PROFILE_NAME,
    ensure_test_filter_profile,
)


def main() -> int:
    with SessionLocal() as db:
        profile, created = ensure_test_filter_profile(db)
    verb = "created" if created else "already exists"
    print(
        f"{verb}: FilterProfile id={profile.id} name={TEST_PROFILE_NAME!r} "
        f"cpv_prefixes={profile.cpv_prefixes}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
