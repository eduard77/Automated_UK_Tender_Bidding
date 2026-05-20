"""portal platforms + platform_id/is_email_domain + recalc

Revision ID: 0006
Revises: 0005
Create Date: 2026-05-20

Phase 4 chunk 2. Introduces the platform-first layer:

* `portal_platforms` — one row per e-tendering *product* (Delta eSourcing,
  ProContract, JAGGAER, ...). A platform owns many buyer-instance portals via
  `domain_patterns` (regexes matched against `portals.domain`).
* `portals.platform_id` — nullable FK; null means "no known platform"
  (buyer-direct gov.uk sites, NHS email domains, etc.).
* `portals.is_email_domain` — true when every sighting for a portal is a
  contact email (e.g. nhs.net): these are not real portals.

Also fixes the v1 tender_count over-count: tender_count is recalculated to
count only distinct tenders linked via tender_link / document_link sightings
(contact_email / reference_text no longer inflate it).
"""
import re
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


LOGIN_TYPES = ("none", "email_only", "username_password", "2fa", "oauth", "unknown")
ADAPTER_STATUSES = ("not_started", "stub", "read_only", "full", "deprecated")
PLATFORM_KINDS = ("transactional", "special")

LINK_SIGHTING_TYPES = ("tender_link", "document_link")


# 7 transactional + 3 special. domain_patterns are Python regexes matched
# against the bare portal domain. Validated against the live DB: each pattern
# set matches >= 3 sightings (see test_portal_platform_matching.py).
PLATFORM_SEEDS: tuple[dict, ...] = (
    {
        "slug": "delta_esourcing",
        "vendor": "BiP Solutions",
        "display_name": "Delta eSourcing",
        "kind": "transactional",
        "login_type": "username_password",
        "domain_patterns": [r"(^|\.)delta-esourcing\.com$"],
        "notes": "Aggregator hosting many buyer instances on subdomains.",
    },
    {
        "slug": "procontract",
        "vendor": "Proactis (Due North)",
        "display_name": "ProContract",
        "kind": "transactional",
        "login_type": "username_password",
        "domain_patterns": [r"(^|\.)due-north\.com$"],
        "notes": "Due North ProContract; buyer instances on subdomains.",
    },
    {
        "slug": "jaggaer",
        "vendor": "JAGGAER",
        "display_name": "JAGGAER (incl. BravoSolution)",
        "kind": "transactional",
        "login_type": "username_password",
        "domain_patterns": [
            r"(^|\.)jaggaer\.com$",
            r"(^|\.)bravosolution\.(com|co\.uk)$",
        ],
        "notes": "Formerly BravoSolution; gov instances on *.app.jaggaer.com.",
    },
    {
        "slug": "in_tend",
        "vendor": "In-tend Ltd",
        "display_name": "In-tend",
        "kind": "transactional",
        "login_type": "username_password",
        "domain_patterns": [r"(^|\.)in-?tend(host)?\.co\.uk$"],
        "notes": "In-tend hosted buyer portals.",
    },
    {
        "slug": "multiquote",
        "vendor": "MultiQuote Ltd",
        "display_name": "MultiQuote",
        "kind": "transactional",
        "login_type": "username_password",
        "domain_patterns": [r"(^|\.)multiquote\.com$"],
        "notes": "NHS-heavy quoting platform.",
    },
    {
        "slug": "eu_supply",
        "vendor": "EU Supply (Mercell)",
        "display_name": "EU Supply",
        "kind": "transactional",
        "login_type": "username_password",
        "domain_patterns": [r"(^|\.)eu-supply\.com$"],
        "notes": "Mercell-owned CTM platform.",
    },
    {
        "slug": "proactis",
        "vendor": "Proactis",
        "display_name": "Proactis P2P",
        "kind": "transactional",
        "login_type": "username_password",
        "domain_patterns": [r"(^|\.)proactisp2p\.com$", r"(^|\.)proactis\.com$"],
        "notes": "Proactis supplier network (distinct from ProContract).",
    },
    {
        "slug": "atamis",
        "vendor": "Atamis",
        "display_name": "Atamis (Salesforce)",
        "kind": "special",
        "login_type": "oauth",
        "domain_patterns": [
            r"(^|\.)atamis\.co\.uk$",
            r"atamis-\d+\.my\.(site|salesforce-sites)\.com$",
        ],
        "notes": "Salesforce-hosted; auth differs from standard portals.",
    },
    {
        "slug": "mytenders",
        "vendor": "Millstream",
        "display_name": "myTenders / Millstream",
        "kind": "special",
        "login_type": "username_password",
        "domain_patterns": [
            r"(^|\.)mytenders\.co\.uk$",
            r"(^|\.)eastmidstenders\.org$",
            r"(^|\.)londontenders\.org$",
            r"(^|\.)supplyingthesouthwest\.org\.uk$",
        ],
        "notes": "Millstream regional aggregator network.",
    },
    {
        "slug": "sproc_net",
        "vendor": "Adam HTT",
        "display_name": "sproc.net",
        "kind": "special",
        "login_type": "username_password",
        "domain_patterns": [r"(^|\.)sproc\.net$"],
        "notes": "Social-care dynamic purchasing system; invite-based.",
    },
)


def _matches(domain: str, patterns: list[str]) -> bool:
    return any(re.search(p, domain) for p in patterns)


def upgrade() -> None:
    op.create_table(
        "portal_platforms",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("vendor", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column(
            "domain_patterns",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("kind", sa.Text(), nullable=False, server_default="transactional"),
        sa.Column(
            "adapter_status",
            sa.Text(),
            nullable=False,
            server_default="not_started",
        ),
        sa.Column("adapter_module", sa.Text()),
        sa.Column("login_type", sa.Text(), nullable=False, server_default="unknown"),
        sa.Column("notes", sa.Text()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "login_type IN ({})".format(", ".join(f"'{v}'" for v in LOGIN_TYPES)),
            name="ck_portal_platforms_login_type",
        ),
        sa.CheckConstraint(
            "adapter_status IN ({})".format(
                ", ".join(f"'{v}'" for v in ADAPTER_STATUSES)
            ),
            name="ck_portal_platforms_adapter_status",
        ),
        sa.CheckConstraint(
            "kind IN ({})".format(", ".join(f"'{v}'" for v in PLATFORM_KINDS)),
            name="ck_portal_platforms_kind",
        ),
    )
    op.create_index(
        "ix_portal_platforms_slug", "portal_platforms", ["slug"], unique=True
    )
    op.create_index(
        "ix_portal_platforms_adapter_status", "portal_platforms", ["adapter_status"]
    )

    op.add_column(
        "portals",
        sa.Column("platform_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "portals",
        sa.Column(
            "is_email_domain",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.create_foreign_key(
        "fk_portals_platform_id",
        "portals",
        "portal_platforms",
        ["platform_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_portals_platform_id", "portals", ["platform_id"])

    # --- seed platforms ---
    platforms_table = sa.table(
        "portal_platforms",
        sa.column("slug", sa.Text()),
        sa.column("vendor", sa.Text()),
        sa.column("display_name", sa.Text()),
        sa.column("domain_patterns", postgresql.JSONB()),
        sa.column("kind", sa.Text()),
        sa.column("login_type", sa.Text()),
        sa.column("notes", sa.Text()),
    )
    op.bulk_insert(
        platforms_table,
        [
            {
                "slug": s["slug"],
                "vendor": s["vendor"],
                "display_name": s["display_name"],
                "domain_patterns": s["domain_patterns"],
                "kind": s["kind"],
                "login_type": s["login_type"],
                "notes": s["notes"],
            }
            for s in PLATFORM_SEEDS
        ],
    )

    bind = op.get_bind()

    # --- backfill platform_id by matching each portal domain ---
    platform_rows = bind.execute(
        sa.text("SELECT id, slug FROM portal_platforms")
    ).fetchall()
    slug_to_id = {row.slug: row.id for row in platform_rows}
    patterns_by_id = {
        slug_to_id[s["slug"]]: s["domain_patterns"] for s in PLATFORM_SEEDS
    }

    portal_rows = bind.execute(sa.text("SELECT id, domain FROM portals")).fetchall()
    for portal in portal_rows:
        for platform_id, patterns in patterns_by_id.items():
            if _matches(portal.domain, patterns):
                bind.execute(
                    sa.text("UPDATE portals SET platform_id = :pid WHERE id = :id"),
                    {"pid": platform_id, "id": portal.id},
                )
                break

    # --- recalc tender_count: count distinct tenders linked via link-type
    #     sightings only (contact_email / reference_text no longer count) ---
    link_in = ", ".join(f"'{t}'" for t in LINK_SIGHTING_TYPES)
    bind.execute(
        sa.text(
            f"""
            UPDATE portals p SET tender_count = COALESCE(sub.cnt, 0)
            FROM (
                SELECT portal_id, COUNT(DISTINCT tender_id) AS cnt
                FROM portal_url_sightings
                WHERE sighting_type IN ({link_in}) AND portal_id IS NOT NULL
                GROUP BY portal_id
            ) sub
            WHERE p.id = sub.portal_id
            """
        )
    )
    # portals with no link-type sightings → tender_count = 0
    bind.execute(
        sa.text(
            f"""
            UPDATE portals SET tender_count = 0
            WHERE id NOT IN (
                SELECT DISTINCT portal_id FROM portal_url_sightings
                WHERE sighting_type IN ({link_in}) AND portal_id IS NOT NULL
            )
            """
        )
    )

    # --- is_email_domain: every sighting is a contact_email ---
    bind.execute(
        sa.text(
            """
            UPDATE portals SET is_email_domain = true
            WHERE id IN (
                SELECT portal_id FROM portal_url_sightings
                WHERE portal_id IS NOT NULL
                GROUP BY portal_id
                HAVING COUNT(*) FILTER (WHERE sighting_type <> 'contact_email') = 0
                   AND COUNT(*) FILTER (WHERE sighting_type = 'contact_email') > 0
            )
            """
        )
    )


def downgrade() -> None:
    op.drop_index("ix_portals_platform_id", table_name="portals")
    op.drop_constraint("fk_portals_platform_id", "portals", type_="foreignkey")
    op.drop_column("portals", "is_email_domain")
    op.drop_column("portals", "platform_id")
    op.drop_index("ix_portal_platforms_adapter_status", table_name="portal_platforms")
    op.drop_index("ix_portal_platforms_slug", table_name="portal_platforms")
    op.drop_table("portal_platforms")
