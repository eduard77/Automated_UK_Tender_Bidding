"""portal discovery + registry

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-19

Phase 4 chunk 1. Two relational tables (portals, portal_url_sightings) plus a
seeded blocklist (portal_blocklist_domains). The blocklist is seeded with the
five UK source domains we already poll (so URLs back to Contracts Finder /
Find a Tender don't get clustered as portals) plus common noise (social media,
big-tech root domains) and the bare `gov.uk` apex.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


LOGIN_TYPES = ("none", "email_only", "username_password", "2fa", "oauth", "unknown")
ADAPTER_STATUSES = ("not_started", "stub", "read_only", "full", "deprecated")
PRIORITIES = ("critical", "high", "medium", "low")
EXTRACTED_FROM_VALUES = (
    "description",
    "additional_information",
    "documents",
    "contact",
    "parties",
)
SIGHTING_TYPES = ("tender_link", "document_link", "contact_email", "reference_text")


SEED_BLOCKLIST: tuple[tuple[str, str], ...] = (
    # UK tender source domains — we ingest from these, so URLs back into them
    # are not "external portals" and would only pollute the registry.
    ("contractsfinder.service.gov.uk", "Contracts Finder — source we poll"),
    ("find-tender.service.gov.uk", "Find a Tender — source we poll"),
    ("publiccontractsscotland.gov.uk", "Public Contracts Scotland — source we poll"),
    ("sell2wales.gov.wales", "Sell2Wales — source we poll"),
    ("etendersni.gov.uk", "eTenders NI — source we poll"),
    # Social
    ("twitter.com", "Social media"),
    ("x.com", "Social media"),
    ("linkedin.com", "Social media"),
    ("facebook.com", "Social media"),
    ("youtube.com", "Video / social"),
    # Big tech roots — usually login or marketing pages, never tender portals
    ("google.com", "Search / marketing"),
    ("microsoft.com", "Vendor marketing"),
    ("office.com", "Vendor marketing"),
    ("bing.com", "Search"),
    # The gov.uk apex — informational pages, policy, news. Specific procurement
    # subdomains (e.g. crowncommercial.gov.uk, nepo.gov.uk) are NOT blocked.
    ("gov.uk", "gov.uk apex — informational only; subdomains allowed"),
)


def upgrade() -> None:
    op.create_table(
        "portals",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("domain", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column(
            "url_patterns",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "login_type",
            sa.Text(),
            nullable=False,
            server_default="unknown",
        ),
        sa.Column(
            "adapter_status",
            sa.Text(),
            nullable=False,
            server_default="not_started",
        ),
        sa.Column("adapter_module", sa.Text()),
        sa.Column(
            "priority",
            sa.Text(),
            nullable=False,
            server_default="medium",
        ),
        sa.Column(
            "tender_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "classification_data",
            postgresql.JSONB(astext_type=sa.Text()),
        ),
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
            name="ck_portals_login_type",
        ),
        sa.CheckConstraint(
            "adapter_status IN ({})".format(
                ", ".join(f"'{v}'" for v in ADAPTER_STATUSES)
            ),
            name="ck_portals_adapter_status",
        ),
        sa.CheckConstraint(
            "priority IN ({})".format(", ".join(f"'{v}'" for v in PRIORITIES)),
            name="ck_portals_priority",
        ),
    )
    op.create_index(
        "ix_portals_domain", "portals", ["domain"], unique=True
    )
    op.create_index("ix_portals_adapter_status", "portals", ["adapter_status"])
    op.create_index("ix_portals_priority", "portals", ["priority"])
    op.create_index(
        "ix_portals_last_seen_at",
        "portals",
        [sa.text("last_seen_at DESC")],
    )

    op.create_table(
        "portal_url_sightings",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "portal_id",
            sa.BigInteger(),
            sa.ForeignKey("portals.id", ondelete="CASCADE"),
        ),
        sa.Column(
            "tender_id",
            sa.BigInteger(),
            sa.ForeignKey("tenders.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("extracted_from", sa.Text(), nullable=False),
        sa.Column("sighting_type", sa.Text(), nullable=False),
        sa.Column(
            "extracted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "extracted_from IN ({})".format(
                ", ".join(f"'{v}'" for v in EXTRACTED_FROM_VALUES)
            ),
            name="ck_portal_url_sightings_extracted_from",
        ),
        sa.CheckConstraint(
            "sighting_type IN ({})".format(
                ", ".join(f"'{v}'" for v in SIGHTING_TYPES)
            ),
            name="ck_portal_url_sightings_sighting_type",
        ),
    )
    op.create_index(
        "ix_portal_url_sightings_portal_id",
        "portal_url_sightings",
        ["portal_id"],
    )
    op.create_index(
        "ix_portal_url_sightings_tender_id",
        "portal_url_sightings",
        ["tender_id"],
    )
    op.create_index(
        "ix_portal_url_sightings_portal_extracted",
        "portal_url_sightings",
        ["portal_id", sa.text("extracted_at DESC")],
    )

    op.create_table(
        "portal_blocklist_domains",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("domain", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text()),
        sa.Column(
            "added_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "added_by",
            sa.Text(),
            nullable=False,
            server_default="system",
        ),
    )
    op.create_index(
        "ix_portal_blocklist_domain",
        "portal_blocklist_domains",
        ["domain"],
        unique=True,
    )

    blocklist_table = sa.table(
        "portal_blocklist_domains",
        sa.column("domain", sa.Text()),
        sa.column("reason", sa.Text()),
        sa.column("added_by", sa.Text()),
    )
    op.bulk_insert(
        blocklist_table,
        [
            {"domain": d, "reason": r, "added_by": "system"}
            for d, r in SEED_BLOCKLIST
        ],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_portal_blocklist_domain", table_name="portal_blocklist_domains"
    )
    op.drop_table("portal_blocklist_domains")
    op.drop_index(
        "ix_portal_url_sightings_portal_extracted",
        table_name="portal_url_sightings",
    )
    op.drop_index(
        "ix_portal_url_sightings_tender_id", table_name="portal_url_sightings"
    )
    op.drop_index(
        "ix_portal_url_sightings_portal_id", table_name="portal_url_sightings"
    )
    op.drop_table("portal_url_sightings")
    op.drop_index("ix_portals_last_seen_at", table_name="portals")
    op.drop_index("ix_portals_priority", table_name="portals")
    op.drop_index("ix_portals_adapter_status", table_name="portals")
    op.drop_index("ix_portals_domain", table_name="portals")
    op.drop_table("portals")
