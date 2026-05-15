#!/usr/bin/env python3
"""Backfill FilterMatch rows for tenders that pre-date a filter profile.

The ingestion pipeline scores each tender against every active filter profile
at upsert time and inserts a FilterMatch row per match. Filter profiles
created AFTER tenders were ingested therefore have no matches — even when the
data clearly satisfies them. This script scores every active filter profile
against every tender in the database, inserts missing FilterMatch rows, and
leaves existing matches untouched.

Usage:
    # Show what would be inserted without writing anything.
    python scripts/backfill_filter_matches.py --dry-run

    # Insert missing matches.
    python scripts/backfill_filter_matches.py

    # Limit to a single profile by id (useful for testing).
    python scripts/backfill_filter_matches.py --filter-id 2

Notes:
    - Existing FilterMatch rows are never deleted or modified.
    - Duplicate tenders (Tender.duplicate_of_id IS NOT NULL) are skipped, the
      same as in services/ingestion.py._record_filter_matches.
    - Score is set to 1.0 for every inserted row (the same default the runtime
      ingest path uses; the filter engine is currently boolean).
    - Inserts are flushed in batches of 100 to keep transaction size sane.
"""
from __future__ import annotations

import argparse
import sys
from collections.abc import Iterator
from pathlib import Path

# Make `tender_agent` importable when run from the repo root or scripts/.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from tender_agent.db import SessionLocal  # noqa: E402
from tender_agent.models import FilterMatch, FilterProfile, Tender  # noqa: E402
from tender_agent.services.filter_engine import matches as filter_matches  # noqa: E402

BATCH_SIZE = 100


def _iter_tenders(db: Session, batch_size: int = 500) -> Iterator[Tender]:
    """Yield every non-duplicate tender, streamed in chunks of `batch_size`.

    `yield_per` keeps memory bounded on the ~thousands-of-rows scale we have
    today without holding the whole result set in memory.
    """
    stmt = (
        select(Tender)
        .where(Tender.duplicate_of_id.is_(None))
        .execution_options(yield_per=batch_size)
    )
    for tender in db.scalars(stmt):
        yield tender


def _already_matched_ids(db: Session, filter_profile_id: int) -> set[int]:
    """Return the set of tender_ids that already have a FilterMatch for this profile."""
    rows = db.execute(
        select(FilterMatch.tender_id).where(
            FilterMatch.filter_profile_id == filter_profile_id
        )
    ).all()
    return {row[0] for row in rows}


def backfill_for_profile(
    db: Session, profile: FilterProfile, *, dry_run: bool
) -> dict[str, int]:
    """Score every tender against one profile, insert missing matches.

    Returns counts for reporting:
        scored:   tenders considered (excludes duplicates)
        matched:  tenders that satisfy the profile
        skipped:  matched tenders already in filter_matches
        inserted: new FilterMatch rows added (or would-add in dry-run)
    """
    existing = _already_matched_ids(db, profile.id)

    scored = matched = skipped = inserted = 0
    pending: list[FilterMatch] = []

    for tender in _iter_tenders(db):
        scored += 1
        if not filter_matches(profile, tender):
            continue
        matched += 1
        if tender.id in existing:
            skipped += 1
            continue
        if dry_run:
            inserted += 1
            continue
        pending.append(
            FilterMatch(
                tender_id=tender.id,
                filter_profile_id=profile.id,
                score=1.0,
            )
        )
        if len(pending) >= BATCH_SIZE:
            db.add_all(pending)
            db.commit()
            inserted += len(pending)
            pending.clear()

    if not dry_run and pending:
        db.add_all(pending)
        db.commit()
        inserted += len(pending)

    return {
        "scored": scored,
        "matched": matched,
        "skipped": skipped,
        "inserted": inserted,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Score and report without inserting any FilterMatch rows.",
    )
    parser.add_argument(
        "--filter-id",
        type=int,
        default=None,
        help="Limit to a single FilterProfile by id (default: all enabled).",
    )
    args = parser.parse_args()

    with SessionLocal() as db:
        profile_stmt = select(FilterProfile).where(FilterProfile.enabled.is_(True))
        if args.filter_id is not None:
            profile_stmt = profile_stmt.where(FilterProfile.id == args.filter_id)
        profiles = list(db.scalars(profile_stmt))

        if not profiles:
            print("No active filter profiles found. Nothing to backfill.")
            return 0

        mode = "DRY-RUN" if args.dry_run else "LIVE"
        print(f"[{mode}] Backfilling matches for {len(profiles)} active filter(s)\n")

        totals: list[tuple[FilterProfile, dict[str, int]]] = []
        for profile in profiles:
            counts = backfill_for_profile(db, profile, dry_run=args.dry_run)
            totals.append((profile, counts))
            print(
                f"  filter id={profile.id} '{profile.name}': "
                f"scored={counts['scored']}, "
                f"matched={counts['matched']}, "
                f"already-matched={counts['skipped']}, "
                f"{'would-insert' if args.dry_run else 'inserted'}={counts['inserted']}"
            )

        print()
        print("Final per-filter state:")
        for profile, counts in totals:
            total_in_db = db.execute(
                select(FilterMatch).where(FilterMatch.filter_profile_id == profile.id)
            ).all()
            print(
                f"  filter id={profile.id} '{profile.name}': "
                f"match_count={len(total_in_db)}"
                + (" (unchanged — dry-run)" if args.dry_run else "")
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
