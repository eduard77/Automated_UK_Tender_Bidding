#!/usr/bin/env python3
"""Backfill portal discovery against every tender already in the database.

For each tender, this script runs `process_tender_for_portals` — the same
service the live ingestion pipeline uses. New portals are created lazily;
existing portals get fresh sightings; the tender_count is incremented per
distinct (portal, tender) pair. Re-running the script is safe: sighting
inserts are skipped if an identical row already exists.

Usage:
    docker compose exec app python scripts/backfill_portal_discovery.py
    python scripts/backfill_portal_discovery.py --batch-size 500
    python scripts/backfill_portal_discovery.py --no-classify  # skip queueing

Notes:
    - Classification (Claude call) for newly-created portals is fire-and-
      forget; pass --no-classify to suppress it (useful when ANTHROPIC_API_KEY
      is missing).
    - Batched with simple LIMIT/OFFSET. Server-side cursors are intentionally
      avoided — they caused a connection-state bug in an earlier prompt run.
    - Refuses to run when TENDER_AGENT_ENV=production as a safety net.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from tender_agent.db import SessionLocal  # noqa: E402
from tender_agent.models import Portal, Tender  # noqa: E402
from tender_agent.services.portal_classifier import schedule_classification  # noqa: E402
from tender_agent.services.portal_discovery import process_tender_for_portals  # noqa: E402


DEFAULT_BATCH = 200


def _print_top_portals(db: Session, n: int = 20) -> None:
    rows = db.execute(
        select(Portal.domain, Portal.display_name, Portal.tender_count)
        .order_by(Portal.tender_count.desc())
        .limit(n)
    ).all()
    if not rows:
        print("\nNo portals discovered.")
        return
    print(f"\nTop {len(rows)} portals by tender_count:")
    print(f"  {'rank':>4}  {'tenders':>7}  {'domain':<48}  display_name")
    print("  " + "-" * 100)
    for rank, (domain, display_name, count) in enumerate(rows, start=1):
        print(f"  {rank:>4}  {count:>7}  {domain:<48}  {display_name}")


def backfill(*, batch_size: int, classify: bool) -> int:
    if os.environ.get("TENDER_AGENT_ENV", "").lower() == "production":
        print("Refusing to run: TENDER_AGENT_ENV=production", file=sys.stderr)
        return 2

    start = time.monotonic()
    total_tenders = 0
    total_new_portals = 0
    total_new_sightings = 0
    total_classifications = 0
    queued_seen: set[int] = set()

    with SessionLocal() as db:
        tender_count = db.execute(select(func.count(Tender.id))).scalar_one()
        print(f"Backfilling portal discovery against {tender_count} tenders "
              f"(batch_size={batch_size}, classify={'yes' if classify else 'no'})\n")

        offset = 0
        while True:
            tenders = list(
                db.execute(
                    select(Tender)
                    .order_by(Tender.id.asc())
                    .offset(offset)
                    .limit(batch_size)
                ).scalars().all()
            )
            if not tenders:
                break

            for tender in tenders:
                try:
                    result = process_tender_for_portals(tender, db)
                except Exception as exc:  # noqa: BLE001
                    print(
                        f"  WARN tender_id={tender.id}: {type(exc).__name__}: {exc}",
                        file=sys.stderr,
                    )
                    db.rollback()
                    continue
                total_new_portals += result.new_portals
                total_new_sightings += result.new_sightings
                if classify:
                    for pid in result.portal_ids_queued:
                        if pid not in queued_seen:
                            queued_seen.add(pid)
                            schedule_classification(pid)
                            total_classifications += 1
            db.commit()
            offset += batch_size
            total_tenders = min(offset, tender_count)
            portal_count_so_far = db.execute(select(func.count(Portal.id))).scalar_one()
            print(
                f"Processed {total_tenders}/{tender_count} tenders, "
                f"found {portal_count_so_far} portals so far"
            )

        elapsed = time.monotonic() - start
        print(
            "\nDone. "
            f"tenders_processed={total_tenders}, "
            f"new_portals={total_new_portals}, "
            f"new_sightings={total_new_sightings}, "
            f"classifications_queued={total_classifications}, "
            f"elapsed={elapsed:.1f}s"
        )

        _print_top_portals(db, n=20)

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH,
        help=f"Tenders per batch (default {DEFAULT_BATCH}).",
    )
    parser.add_argument(
        "--no-classify",
        action="store_true",
        help="Do not queue Claude classification for newly-created portals.",
    )
    args = parser.parse_args()
    return backfill(batch_size=args.batch_size, classify=not args.no_classify)


if __name__ == "__main__":
    raise SystemExit(main())
