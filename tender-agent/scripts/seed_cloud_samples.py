"""Seed a small set of clearly-marked SAMPLE tenders into the CLOUD DB
for funnel testing. Idempotent (re-runs UPSERT, never duplicate), reversible
(--clear removes exactly the rows it inserted), and cloud-only (never touches
local Postgres).

The cloud connection is built using the SAME helpers as build_cloud_schema.py
(.env parsing, URL construction, psycopg connect) — those functions are
imported from that script so the auth/SSL behaviour stays consistent.

Marker convention:
  - Tenders:  source_code = 'SAMPLE_SEED'
              source_ref  = 'SAMPLE-NNN'   (deterministic per sample row)
              title       starts with '[SAMPLE]'
  - Brief:    tender_briefs.model = 'sample-seed/v1'

Usage:
    python scripts/seed_cloud_samples.py            # seed / upsert
    python scripts/seed_cloud_samples.py --clear    # remove the sample rows
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

# ---------------------------------------------------------------------------
# Reuse the cloud-connection helpers from the sibling build_cloud_schema
# script. They live in the same scripts/ directory; importing them keeps
# the .env-parsing + URL-construction + SSL behaviour in lock-step with
# the schema build.
# ---------------------------------------------------------------------------
HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from build_cloud_schema import (  # noqa: E402  (intentional after sys.path tweak)
    BACKEND_ROOT,
    ENV_FILE,
    cloud_url_for_db,
    die,
    get_target_db_name,
    load_env_file,
    psycopg_connect,
    redact_url,
)


SOURCE_CODE = "SAMPLE_SEED"
BRIEF_MODEL = "sample-seed/v1"


# ---------------------------------------------------------------------------
# The sample tenders. Every row uses Decimal for money so the Numeric(18, 2)
# column doesn't get a float. Deadlines are 'days from now' so re-running
# weeks later still shows a sensible 'days remaining' on the dashboard.
# ---------------------------------------------------------------------------

def _utc(days_from_now: int) -> datetime:
    return datetime.now(UTC) + timedelta(days=days_from_now)


def _published(days_ago: int) -> datetime:
    return datetime.now(UTC) - timedelta(days=days_ago)


def sample_tenders() -> list[dict]:
    return [
        {
            "source_ref": "SAMPLE-001",
            "source_url": "https://example.invalid/sample/001",
            "title": "[SAMPLE] Refurbishment of Birmingham Library Reading Rooms",
            "description": (
                "Refurbishment of three reading rooms at the Library of "
                "Birmingham including replacement carpets, redecoration, "
                "lighting upgrades to LED, and accessibility improvements. "
                "Lead-time critical: works must complete during the autumn "
                "vacancy window."
            ),
            "notice_type": "contract",
            "status": "active",
            "buyer_name": "Birmingham City Council",
            "buyer_country": "United Kingdom",
            "buyer_region": "West Midlands",
            "region": "West Midlands",
            "cpv_codes": ["45454000", "45442100"],
            "keywords": ["refurbishment", "library", "fit-out", "accessibility"],
            "value_amount": Decimal("450000.00"),
            "value_currency": "GBP",
            "published_at": _published(2),
            "deadline_at": _utc(21),
        },
        {
            "source_ref": "SAMPLE-002",
            "source_url": "https://example.invalid/sample/002",
            "title": "[SAMPLE] Highways Maintenance Framework – Greater Manchester",
            "description": (
                "Four-year framework for reactive and planned highway "
                "maintenance across the ten Greater Manchester boroughs. "
                "Lotted by sub-region; multi-supplier award expected."
            ),
            "notice_type": "contract",
            "status": "active",
            "buyer_name": "Transport for Greater Manchester",
            "buyer_country": "United Kingdom",
            "buyer_region": "North West",
            "region": "North West",
            "cpv_codes": ["45233141", "45233140"],
            "keywords": ["highways", "framework", "maintenance"],
            "value_amount": Decimal("8500000.00"),
            "value_currency": "GBP",
            "published_at": _published(6),
            "deadline_at": _utc(35),
        },
        {
            "source_ref": "SAMPLE-003",
            "source_url": "https://example.invalid/sample/003",
            "title": "[SAMPLE] School Mechanical & Electrical Upgrades Lot 3",
            "description": (
                "M&E upgrade works across five Leeds primary schools, "
                "including new boilers, lighting controls and rewires. "
                "Works during summer vacation; phased handover required."
            ),
            "notice_type": "contract",
            "status": "active",
            "buyer_name": "Leeds City Council",
            "buyer_country": "United Kingdom",
            "buyer_region": "Yorkshire and the Humber",
            "region": "Yorkshire and the Humber",
            "cpv_codes": ["45331000", "45310000"],
            "keywords": ["mechanical", "electrical", "schools", "m&e"],
            "value_amount": Decimal("1250000.00"),
            "value_currency": "GBP",
            "published_at": _published(1),
            "deadline_at": _utc(10),
        },
        {
            "source_ref": "SAMPLE-004",
            "source_url": "https://example.invalid/sample/004",
            "title": "[SAMPLE] Cyclical Roof Replacement Programme 2026/27",
            "description": (
                "Replacement of pitched roofs across approximately 180 "
                "social-housing properties in Glasgow. Asbestos surveys "
                "complete; CDM principal-contractor role required."
            ),
            "notice_type": "contract",
            "status": "active",
            "buyer_name": "Glasgow Housing Association",
            "buyer_country": "United Kingdom",
            "buyer_region": "Scotland",
            "region": "Scotland",
            "cpv_codes": ["45261910", "45261000"],
            "keywords": ["roofing", "housing", "cdm"],
            "value_amount": Decimal("2300000.00"),
            "value_currency": "GBP",
            "published_at": _published(4),
            "deadline_at": _utc(18),
        },
        {
            "source_ref": "SAMPLE-005",
            "source_url": "https://example.invalid/sample/005",
            "title": "[SAMPLE] Modular Classroom Block – Cardiff Primary",
            "description": (
                "Design-and-build of a single-storey modular classroom "
                "block providing two general classrooms and a shared "
                "breakout space. Tight programme: open by Easter."
            ),
            "notice_type": "contract",
            "status": "active",
            "buyer_name": "Cardiff Council",
            "buyer_country": "United Kingdom",
            "buyer_region": "Wales",
            "region": "Wales",
            "cpv_codes": ["45214210", "45210000"],
            "keywords": ["modular", "school", "design-and-build"],
            "value_amount": Decimal("680000.00"),
            "value_currency": "GBP",
            "published_at": _published(0),
            "deadline_at": _utc(5),
        },
        {
            "source_ref": "SAMPLE-006",
            "source_url": "https://example.invalid/sample/006",
            "title": "[SAMPLE] Net-Zero Retrofit – Tower Hamlets Estates Phase 2",
            "description": (
                "Deep retrofit of three council estates targeting EPC B, "
                "including external wall insulation, MVHR, PV, and "
                "communal-heating decarbonisation. Resident-in-occupation "
                "phasing; PAS 2035 compliant."
            ),
            "notice_type": "contract",
            "status": "active",
            "buyer_name": "London Borough of Tower Hamlets",
            "buyer_country": "United Kingdom",
            "buyer_region": "London",
            "region": "London",
            "cpv_codes": ["45262690", "45321000"],
            "keywords": ["retrofit", "net-zero", "pas2035", "housing"],
            "value_amount": Decimal("14750000.00"),
            "value_currency": "GBP",
            "published_at": _published(8),
            "deadline_at": _utc(60),
        },
    ]


def sample_brief_json(tender: dict) -> dict:
    """Hand-crafted brief that matches BriefSchema exactly (services/brief/
    brief_generator.py:BriefSchema). Schema-valid so the dashboard renders
    every section; clearly synthetic so it's safe to delete later."""
    value_str = f"£{tender['value_amount']:,.0f} (estimated)"
    deadline_iso = tender["deadline_at"].date().isoformat()
    return {
        "recommendation": "bid",
        "confidence": "medium",
        "headline": (
            f"Mid-size {tender['region']} refurbishment with a tight autumn "
            "window — BID if estimating capacity available next week."
        ),
        "rationale": (
            "Scope is well-defined, value sits comfortably within our PI cover, "
            "and the buyer has run similar lots in the last 18 months with "
            "predictable evaluation patterns. Programme risk is the main "
            "consideration: the autumn vacancy window is non-negotiable, so we "
            "must price preliminaries and out-of-hours working accurately."
        ),
        "key_risks": [
            {
                "risk": "Fixed-date completion in autumn vacancy window",
                "severity": "high",
                "detail": (
                    "Liquidated damages likely; mitigate with phased handover "
                    "and a pre-booked second crew on standby."
                ),
            },
            {
                "risk": "Accessibility compliance to current Equality Act guidance",
                "severity": "medium",
                "detail": (
                    "Spec references 'current guidance' rather than a dated "
                    "standard. Clarify version with buyer before pricing."
                ),
            },
            {
                "risk": "Existing asbestos surveys not provided with the ITT",
                "severity": "medium",
                "detail": (
                    "Mandatory clarification — bid price is exposed without "
                    "the R&D survey results."
                ),
            },
        ],
        "deadline": {
            "date": deadline_iso,
            "note": "Standard noon UK time submission deadline assumed.",
        },
        "contract_value": {
            "amount": value_str,
            "note": (
                "Indicative budget stated in the OJEU notice; pricing schedule "
                "expected to be priced against a BoQ."
            ),
        },
        "mandatory_requirements": [
            "ISO 9001 quality management certification",
            "ISO 14001 environmental management certification",
            "Constructionline Gold or equivalent SSIP accreditation",
            "Employer's Liability cover of at least £10m",
            "Public Liability cover of at least £5m",
            "Two reference projects of similar scope completed in the last 3 years",
        ],
        "scoring": {
            "summary": "60% Quality / 40% Price (typical for this buyer's framework lots).",
            "criteria": [
                {"criterion": "Method statement and programme", "weight": "25%"},
                {"criterion": "Quality management approach", "weight": "15%"},
                {"criterion": "Social value plan", "weight": "10%"},
                {"criterion": "Health & safety and CDM compliance", "weight": "10%"},
                {"criterion": "Total tendered price", "weight": "40%"},
            ],
        },
        "scope_summary": (
            "Three reading rooms refurbished to a current accessibility "
            "standard: full strip-out, replacement floor coverings, "
            "redecoration, LED lighting with daylight sensors, and minor "
            "alterations to circulation routes. Furniture, IT cabling, "
            "and shelving are excluded. Works to be carried out during a "
            "single autumn vacancy block."
        ),
        "notable_conditions": [
            "10% retention released at practical completion + 12 months",
            "Performance bond required from a UK clearing bank",
            "Parent company guarantee if turnover ratio < 2x annual value",
        ],
        "missing_or_unclear": [
            "Dated reference for the accessibility standard (Equality Act guidance year)",
            "Asbestos R&D survey scope and results",
            "Confirmation of out-of-hours working permitted under the noise consent",
        ],
    }


# ---------------------------------------------------------------------------
# Database operations
# ---------------------------------------------------------------------------

_UPSERT_TENDER_SQL = """
INSERT INTO tenders (
    source_code, source_ref, source_url, title, description,
    notice_type, status, buyer_name, buyer_country, buyer_region, region,
    cpv_codes, keywords,
    value_amount, value_currency,
    published_at, deadline_at,
    first_seen_at, last_seen_at
) VALUES (
    %(source_code)s, %(source_ref)s, %(source_url)s, %(title)s, %(description)s,
    %(notice_type)s, %(status)s, %(buyer_name)s, %(buyer_country)s, %(buyer_region)s, %(region)s,
    %(cpv_codes)s, %(keywords)s,
    %(value_amount)s, %(value_currency)s,
    %(published_at)s, %(deadline_at)s,
    %(now)s, %(now)s
)
ON CONFLICT (source_code, source_ref) DO UPDATE SET
    source_url     = EXCLUDED.source_url,
    title          = EXCLUDED.title,
    description    = EXCLUDED.description,
    notice_type    = EXCLUDED.notice_type,
    status         = EXCLUDED.status,
    buyer_name     = EXCLUDED.buyer_name,
    buyer_country  = EXCLUDED.buyer_country,
    buyer_region   = EXCLUDED.buyer_region,
    region         = EXCLUDED.region,
    cpv_codes      = EXCLUDED.cpv_codes,
    keywords       = EXCLUDED.keywords,
    value_amount   = EXCLUDED.value_amount,
    value_currency = EXCLUDED.value_currency,
    published_at   = EXCLUDED.published_at,
    deadline_at    = EXCLUDED.deadline_at,
    last_seen_at   = EXCLUDED.last_seen_at
RETURNING id
"""

_UPSERT_SOURCE_SQL = """
INSERT INTO sources (code, name, base_url, enabled, created_at)
VALUES (%(code)s, %(name)s, %(base_url)s, TRUE, %(now)s)
ON CONFLICT (code) DO UPDATE SET
    name = EXCLUDED.name,
    base_url = EXCLUDED.base_url
RETURNING id
"""


def seed(conn) -> dict:
    now = datetime.now(UTC)
    summary = {"tenders": [], "brief_tender_id": None}

    with conn.cursor() as cur:
        # 1) Ensure a row in 'sources' for our marker, so dashboards that
        # derive "sources active" from this table show something sensible.
        cur.execute(
            _UPSERT_SOURCE_SQL,
            {
                "code": SOURCE_CODE,
                "name": "Sample seed (manual)",
                "base_url": "https://example.invalid/sample",
                "now": now,
            },
        )

        # 2) UPSERT each sample tender, capturing the assigned IDs.
        tender_ids: dict[str, int] = {}
        for row in sample_tenders():
            params = {
                "source_code": SOURCE_CODE,
                "now": now,
                **row,
            }
            cur.execute(_UPSERT_TENDER_SQL, params)
            tid = cur.fetchone()[0]
            tender_ids[row["source_ref"]] = tid
            summary["tenders"].append(
                {
                    "id": tid,
                    "source_ref": row["source_ref"],
                    "title": row["title"],
                    "value": str(row["value_amount"]),
                    "deadline": row["deadline_at"].date().isoformat(),
                }
            )

        # 3) Brief — one row for SAMPLE-001 (the Birmingham refurb).
        # Idempotent: delete any prior sample brief for this tender first
        # (only those with our marker model), then insert a fresh one.
        target_ref = "SAMPLE-001"
        target_tid = tender_ids[target_ref]
        target_tender = next(t for t in sample_tenders() if t["source_ref"] == target_ref)
        brief_obj = sample_brief_json(target_tender)

        cur.execute(
            "DELETE FROM tender_briefs WHERE tender_id = %s AND model = %s",
            (target_tid, BRIEF_MODEL),
        )
        cur.execute(
            """
            INSERT INTO tender_briefs (
                tender_id, status, recommendation, confidence, headline,
                brief_json, model, generated_at, created_at, updated_at
            )
            VALUES (
                %s, 'complete', %s, %s, %s,
                %s::jsonb, %s, %s, %s, %s
            )
            """,
            (
                target_tid,
                brief_obj["recommendation"],
                brief_obj["confidence"],
                brief_obj["headline"],
                json.dumps(brief_obj),
                BRIEF_MODEL,
                now,
                now,
                now,
            ),
        )
        summary["brief_tender_id"] = target_tid

    conn.commit()
    return summary


def clear(conn) -> dict:
    deleted = {"briefs": 0, "tenders": 0, "sources": 0}
    with conn.cursor() as cur:
        # Briefs first (FK to tenders), then tenders, then the sources row.
        # Restrict tightly to our marker so nothing else can be hit.
        cur.execute(
            """
            DELETE FROM tender_briefs
            WHERE model = %s
              AND tender_id IN (
                  SELECT id FROM tenders WHERE source_code = %s
              )
            """,
            (BRIEF_MODEL, SOURCE_CODE),
        )
        deleted["briefs"] = cur.rowcount
        cur.execute("DELETE FROM tenders WHERE source_code = %s", (SOURCE_CODE,))
        deleted["tenders"] = cur.rowcount
        cur.execute("DELETE FROM sources WHERE code = %s", (SOURCE_CODE,))
        deleted["sources"] = cur.rowcount
    conn.commit()
    return deleted


def verify(conn) -> None:
    print("\n=== verification ===")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM tenders WHERE source_code = %s",
            (SOURCE_CODE,),
        )
        n_tenders = cur.fetchone()[0]
        print(f"sample tenders in cloud DB: {n_tenders}")

        cur.execute(
            """
            SELECT source_ref, title, value_amount, value_currency, deadline_at
            FROM tenders
            WHERE source_code = %s
            ORDER BY source_ref
            """,
            (SOURCE_CODE,),
        )
        print(f"\n{'source_ref':<14}{'value':>16}  deadline    title")
        print("-" * 100)
        for ref, title, val, cur_, dl in cur.fetchall():
            print(
                f"{ref:<14}{cur_} {val:>12,.0f}  "
                f"{dl.date().isoformat()}  {title}"
            )

        cur.execute(
            """
            SELECT b.id, b.tender_id, b.status, b.recommendation, b.model,
                   b.headline,
                   jsonb_object_keys(b.brief_json) AS k
            FROM tender_briefs b
            JOIN tenders t ON t.id = b.tender_id
            WHERE t.source_code = %s
              AND b.model = %s
            """,
            (SOURCE_CODE, BRIEF_MODEL),
        )
        brief_rows = cur.fetchall()
        if not brief_rows:
            print("\n[warn] no sample brief found")
        else:
            keys = sorted({r[6] for r in brief_rows})
            first = brief_rows[0]
            print(
                f"\nsample brief: id={first[0]} tender_id={first[1]} "
                f"status={first[2]} recommendation={first[3]} model={first[4]}"
            )
            print(f"  headline: {first[5]}")
            print(f"  brief_json keys: {keys}")
            expected = {
                "recommendation", "confidence", "headline", "rationale",
                "key_risks", "deadline", "contract_value",
                "mandatory_requirements", "scoring", "scope_summary",
                "notable_conditions", "missing_or_unclear",
            }
            missing = expected - set(keys)
            extra = set(keys) - expected
            if missing:
                print(f"  [warn] brief_json missing keys: {sorted(missing)}")
            else:
                print("  [ok] brief_json contains every BriefSchema field")
            if extra:
                print(f"  [info] brief_json has additional keys: {sorted(extra)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--clear",
        action="store_true",
        help="remove all sample rows (idempotent reverse of seed)",
    )
    args = parser.parse_args()

    env = load_env_file(ENV_FILE)
    host = env.get("CLOUD_DB_HOST")
    user = env.get("CLOUD_DB_USER")
    password = env.get("CLOUD_DB_PASSWORD")
    missing = [k for k, v in (
        ("CLOUD_DB_HOST", host),
        ("CLOUD_DB_USER", user),
        ("CLOUD_DB_PASSWORD", password),
    ) if not v]
    if missing:
        die(f"BLOCKED: missing required env keys in {ENV_FILE}: {missing}")

    target_db = get_target_db_name(env)
    target_url = cloud_url_for_db(user, password, host, target_db)

    print(f"[cfg] backend root: {BACKEND_ROOT}")
    print(f"[cfg] target db:    {target_db}")
    print(f"[cfg] target URL:   {redact_url(target_url)}")
    print(f"[cfg] marker:       source_code='{SOURCE_CODE}', brief model='{BRIEF_MODEL}'")

    conn = psycopg_connect(target_url)
    try:
        if args.clear:
            print("\n[run] CLEAR — removing sample seed rows")
            deleted = clear(conn)
            print(f"[ok] deleted briefs: {deleted['briefs']}")
            print(f"[ok] deleted tenders: {deleted['tenders']}")
            print(f"[ok] deleted sources rows: {deleted['sources']}")
            print("\n[done] sample data cleared.")
        else:
            print("\n[run] SEED — upserting sample tenders + one brief")
            summary = seed(conn)
            print(f"[ok] upserted {len(summary['tenders'])} sample tenders")
            print(f"[ok] sample brief on tender_id={summary['brief_tender_id']}")
            print("[path] brief generated via: hand-crafted schema-valid JSON "
                  "(no LLM call — sample tenders have no real documents).")
            verify(conn)
            print(
                "\n[done] Sample seed complete — cloud only, idempotent, reversible.\n"
                "       To clear later: "
                "python scripts/seed_cloud_samples.py --clear"
            )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
