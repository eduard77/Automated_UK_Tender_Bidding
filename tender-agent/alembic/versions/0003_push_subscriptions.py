"""add push_subscriptions table

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-12
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "push_subscriptions",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "filter_profile_id",
            sa.Integer(),
            sa.ForeignKey("filter_profiles.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("endpoint", sa.Text(), nullable=False, unique=True),
        sa.Column("p256dh", sa.Text(), nullable=False),
        sa.Column("auth", sa.Text(), nullable=False),
        sa.Column("user_agent", sa.Text()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
    )
    op.create_index(
        "ix_push_subscriptions_filter_profile_id",
        "push_subscriptions",
        ["filter_profile_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_push_subscriptions_filter_profile_id", table_name="push_subscriptions")
    op.drop_table("push_subscriptions")
