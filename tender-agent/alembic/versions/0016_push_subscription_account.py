"""link push subscriptions to an account (per-user notifications)

Revision ID: 0016
Revises: 0015
Create Date: 2026-06-09

Closes the catch-all push privacy hole: a subscription is now OWNED by an
account, so every notification path can target only that user's devices. The
column is nullable because legacy rows pre-date ownership — those are treated as
unowned and excluded from per-user dispatch (never blasted), not backfilled
here. An operator who wants to keep an existing subscription alive can backfill
it to their own account id manually.

Existing migrations are untouched.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: Union[str, None] = "0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "push_subscriptions",
        sa.Column("account_id", sa.BigInteger(), nullable=True),
    )
    op.create_foreign_key(
        "fk_push_subscriptions_account_id",
        "push_subscriptions",
        "accounts",
        ["account_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_push_subscriptions_account_id",
        "push_subscriptions",
        ["account_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_push_subscriptions_account_id", table_name="push_subscriptions"
    )
    op.drop_constraint(
        "fk_push_subscriptions_account_id",
        "push_subscriptions",
        type_="foreignkey",
    )
    op.drop_column("push_subscriptions", "account_id")
