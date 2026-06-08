#!/usr/bin/env python3
"""Operator diagnostic: how do CF/FTS notices route suppliers onward to portals?

READ-ONLY. This surveys the Contracts Finder / Find a Tender tenders already in
the database and classifies, for each one, how it points a supplier at the real
tender portal. It adds no columns, touches no models, changes no search /
dashboard / ingestion behaviour, and makes no network calls — it only reads
fields we already store (``source_url``, ``description``, the raw OCDS notice
JSON, and the derived ``documents`` list) and parses them.

Every CF/FTS tender lands in exactly one bucket:

  direct_portal_link   clickable URL to the *specific* opportunity on a named
                       portal (Delta accessCode, ProContract advert id, In-tend
                       project id, ...). For these we also record which portal.
  portal_generic_link  URL to a known portal but only its home/login page.
  email_only           no usable link, but a contact email is present.
  none                 neither a usable portal link nor an email.

We use the result to rank which portal adapters are worth building next: the
per-portal breakdown of ``direct_portal_link`` is the headline signal.

The classification logic and the editable portal table live in
``tender_agent.diagnostics.onward_routes`` (re-runnable, unit tested on strings).

Usage:
    python scripts/survey_cf_onward_routes.py
    python scripts/survey_cf_onward_routes.py --output routes.csv
    python scripts/survey_cf_onward_routes.py --limit 200          # spot-check
    python scripts/survey_cf_onward_routes.py --sources CF          # CF only

Prints the headline counts and the CSV path on the final line.
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tender_agent.db import SessionLocal  # noqa: E402
from tender_agent.diagnostics.cf_survey import (  # noqa: E402
    DEFAULT_SOURCES,
    SurveySummary,
    iter_classified,
    summarise,
)
from tender_agent.diagnostics.onward_routes import BUCKETS, RouteClassification  # noqa: E402
from tender_agent.models import Tender  # noqa: E402

DEFAULT_OUTPUT = "cf_onward_routes_survey.csv"
CSV_HEADER = ("tender_id", "title", "buyer", "bucket", "portal", "best_detail")


def write_csv(path: Path, results: list[tuple[Tender, RouteClassification]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(CSV_HEADER)
        for tender, cls in results:
            writer.writerow(
                [
                    tender.id,
                    tender.title or "",
                    tender.buyer_name or "",
                    cls.bucket,
                    cls.portal or "",
                    cls.detail or "",
                ]
            )


def print_summary(summary: SurveySummary, csv_path: Path) -> None:
    total = summary.total_scanned

    print(f"\nScanned {total} CF/FTS tender(s).\n")
    print("Onward-route bucket breakdown:")
    for bucket in BUCKETS:
        n = summary.buckets.get(bucket, 0)
        pct = (100.0 * n / total) if total else 0.0
        print(f"  {bucket:<20} {n:>8}  ({pct:5.1f}%)")

    print("\ndirect_portal_link by portal (adapter-build ranking):")
    if summary.direct_portal_breakdown:
        for portal, n in summary.direct_portal_breakdown:
            print(f"  {portal:<20} {n:>8}")
    else:
        print("  (none)")

    print(f"\nCSV written to: {csv_path}")
    parts = " ".join(f"{b}={summary.buckets.get(b, 0)}" for b in BUCKETS)
    print(f"Headline: {total} CF/FTS scanned — {parts} | CSV: {csv_path}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT,
        help=f"CSV output path (default: {DEFAULT_OUTPUT}).",
    )
    parser.add_argument(
        "--sources",
        default=",".join(DEFAULT_SOURCES),
        help="Comma-separated source codes to scan (default: CF,FTS).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only scan the first N tenders (by id) — handy for a spot-check.",
    )
    args = parser.parse_args()

    sources = tuple(s.strip().upper() for s in args.sources.split(",") if s.strip())
    csv_path = Path(args.output).resolve()

    with SessionLocal() as db:
        results = list(iter_classified(db, sources, args.limit))

    write_csv(csv_path, results)
    print_summary(summarise(results), csv_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
