"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-05-07
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "sources",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(length=32), nullable=False, unique=True),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("base_url", sa.String(length=512), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_polled_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    op.create_table(
        "filter_profiles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("cpv_codes", postgresql.ARRAY(sa.String())),
        sa.Column("cpv_prefixes", postgresql.ARRAY(sa.String())),
        sa.Column("keywords_any", postgresql.ARRAY(sa.String())),
        sa.Column("keywords_all", postgresql.ARRAY(sa.String())),
        sa.Column("keywords_none", postgresql.ARRAY(sa.String())),
        sa.Column("buyer_names", postgresql.ARRAY(sa.String())),
        sa.Column("regions", postgresql.ARRAY(sa.String())),
        sa.Column("countries", postgresql.ARRAY(sa.String())),
        sa.Column("notice_types", postgresql.ARRAY(sa.String())),
        sa.Column("value_min", sa.Numeric(18, 2)),
        sa.Column("value_max", sa.Numeric(18, 2)),
        sa.Column("min_days_to_deadline", sa.Integer()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    op.create_table(
        "tenders",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("source_code", sa.String(length=32), nullable=False),
        sa.Column("source_ref", sa.String(length=256), nullable=False),
        sa.Column("source_url", sa.String(length=1024)),
        sa.Column("procurement_ref", sa.String(length=256)),
        sa.Column("duplicate_of_id", sa.BigInteger(), sa.ForeignKey("tenders.id")),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("notice_type", sa.String(length=64)),
        sa.Column("status", sa.String(length=32)),
        sa.Column("buyer_name", sa.String(length=512)),
        sa.Column("buyer_id", sa.String(length=128)),
        sa.Column("buyer_country", sa.String(length=8)),
        sa.Column("buyer_region", sa.String(length=128)),
        sa.Column("cpv_codes", postgresql.ARRAY(sa.String())),
        sa.Column("keywords", postgresql.ARRAY(sa.String())),
        sa.Column("value_amount", sa.Numeric(18, 2)),
        sa.Column("value_currency", sa.String(length=8)),
        sa.Column("value_min", sa.Numeric(18, 2)),
        sa.Column("value_max", sa.Numeric(18, 2)),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("deadline_at", sa.DateTime(timezone=True)),
        sa.Column("contract_start", sa.Date()),
        sa.Column("contract_end", sa.Date()),
        sa.Column("documents", postgresql.JSONB()),
        sa.Column("raw", postgresql.JSONB()),
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
        sa.Column("content_hash", sa.String(length=64)),
        sa.UniqueConstraint("source_code", "source_ref", name="uq_tender_source_ref"),
    )
    op.create_index("ix_tenders_source_code", "tenders", ["source_code"])
    op.create_index("ix_tenders_procurement_ref", "tenders", ["procurement_ref"])
    op.create_index("ix_tenders_duplicate_of_id", "tenders", ["duplicate_of_id"])
    op.create_index("ix_tenders_published_at", "tenders", ["published_at"])
    op.create_index("ix_tenders_deadline_at", "tenders", ["deadline_at"])
    op.create_index("ix_tenders_value_amount", "tenders", ["value_amount"])
    op.create_index("ix_tenders_buyer_name", "tenders", ["buyer_name"])

    op.create_table(
        "filter_matches",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "tender_id",
            sa.BigInteger(),
            sa.ForeignKey("tenders.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "filter_profile_id",
            sa.Integer(),
            sa.ForeignKey("filter_profiles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("score", sa.Numeric(5, 4), nullable=False, server_default="1.0"),
        sa.Column(
            "matched_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("notified_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("tender_id", "filter_profile_id", name="uq_match_tender_filter"),
    )
    op.create_index("ix_filter_matches_tender_id", "filter_matches", ["tender_id"])
    op.create_index(
        "ix_filter_matches_filter_profile_id", "filter_matches", ["filter_profile_id"]
    )

    op.create_table(
        "poll_runs",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "source_id",
            sa.Integer(),
            sa.ForeignKey("sources.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="running"),
        sa.Column("fetched", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("new_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text()),
    )
    op.create_index("ix_poll_runs_source_id", "poll_runs", ["source_id"])


def downgrade() -> None:
    op.drop_table("poll_runs")
    op.drop_table("filter_matches")
    op.drop_table("tenders")
    op.drop_table("filter_profiles")
    op.drop_table("sources")
