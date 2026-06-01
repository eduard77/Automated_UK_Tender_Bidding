"""accounts + brief_entitlements + submission_package_payments + sessions

Revision ID: 0013
Revises: 0012
Create Date: 2026-06-01

Phase 4 chunk 6. Adds the four tables that back tiered brief access:

* ``accounts`` — registered users (email + hashed password). plan column drives
  monthly metering for plan_100 / plan_unlimited; payg accounts use the
  brief_entitlements table to know what they unlocked.
* ``account_sessions`` — opaque server-side session tokens keyed by an httpOnly
  cookie value. Tokens are NOT JWTs; revocation is just a DELETE.
* ``brief_entitlements`` — (account, tender) -> "may see the FULL brief + FULL
  documents". One row per unlocked tender per user. Source records HOW it was
  granted (payg / plan / dev).
* ``submission_package_payments`` — per-tender, per-account record of the
  upfront 0.5% submission-package fee. Status flips to 'paid' on Stripe
  webhook.

Existing migrations are untouched.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "accounts",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column(
            "plan",
            sa.String(length=24),
            nullable=False,
            server_default=sa.text("'free'"),
        ),
        sa.Column("stripe_customer_id", sa.String(length=64), nullable=True),
        sa.Column(
            "current_period_end", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "brief_generations_this_period",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("period_anchor", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("email", name="uq_accounts_email"),
    )

    op.create_table(
        "account_sessions",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("account_id", sa.BigInteger(), nullable=False),
        sa.Column("token", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["account_id"], ["accounts.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint("token", name="uq_account_sessions_token"),
    )
    op.create_index(
        "ix_account_sessions_account_id", "account_sessions", ["account_id"]
    )

    op.create_table(
        "brief_entitlements",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("account_id", sa.BigInteger(), nullable=False),
        sa.Column("tender_id", sa.BigInteger(), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column(
            "granted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["account_id"], ["accounts.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["tender_id"], ["tenders.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint(
            "account_id", "tender_id", name="uq_brief_entitlement"
        ),
    )
    op.create_index(
        "ix_brief_entitlements_account_id",
        "brief_entitlements",
        ["account_id"],
    )

    op.create_table(
        "submission_package_payments",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("account_id", sa.BigInteger(), nullable=False),
        sa.Column("tender_id", sa.BigInteger(), nullable=False),
        sa.Column("amount_pence", sa.Integer(), nullable=False),
        sa.Column(
            "currency",
            sa.String(length=8),
            nullable=False,
            server_default=sa.text("'GBP'"),
        ),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column(
            "stripe_checkout_session_id", sa.String(length=128), nullable=True
        ),
        sa.Column(
            "stripe_payment_intent_id", sa.String(length=128), nullable=True
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["account_id"], ["accounts.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["tender_id"], ["tenders.id"], ondelete="CASCADE"
        ),
    )
    op.create_index(
        "ix_subpkg_account_tender",
        "submission_package_payments",
        ["account_id", "tender_id"],
    )
    op.create_index(
        "ix_subpkg_checkout_session",
        "submission_package_payments",
        ["stripe_checkout_session_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_subpkg_checkout_session", table_name="submission_package_payments"
    )
    op.drop_index(
        "ix_subpkg_account_tender", table_name="submission_package_payments"
    )
    op.drop_table("submission_package_payments")
    op.drop_index(
        "ix_brief_entitlements_account_id", table_name="brief_entitlements"
    )
    op.drop_table("brief_entitlements")
    op.drop_index(
        "ix_account_sessions_account_id", table_name="account_sessions"
    )
    op.drop_table("account_sessions")
    op.drop_table("accounts")
