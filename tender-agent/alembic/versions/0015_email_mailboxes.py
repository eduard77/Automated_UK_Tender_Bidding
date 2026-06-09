"""email mailboxes + processed messages (Phase 6 §5.8)

Revision ID: 0015
Revises: 0014
Create Date: 2026-06-09

Email integration: connect an inbox over OAuth, watch for tender emails, file
them, draft a reply, notify. Two tables:

* ``mailbox_accounts`` — one OAuth-connected inbox per (account, provider,
  mailbox). The OAuth token is stored encrypted in ``token_ciphertext``
  (Fernet, key from the portal credentials store) and is never logged. Scoped
  per Account from the start — this is the first multi-user-shaped table.
* ``mailbox_messages`` — one row per processed (matched) inbound email. The
  unique (mailbox_account_id, provider_message_id) is the idempotency guard:
  re-processing the same message files nothing twice and notifies no one twice.

Existing migrations are untouched.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015"
down_revision: Union[str, None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "mailbox_accounts",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("account_id", sa.BigInteger(), nullable=False),
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column("email_address", sa.String(length=320), nullable=True),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("token_ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("connect_state", sa.String(length=64), nullable=True),
        sa.Column("last_polled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["account_id"], ["accounts.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint(
            "account_id",
            "provider",
            "email_address",
            name="uq_mailbox_account_provider_email",
        ),
    )
    op.create_index(
        "ix_mailbox_accounts_account_id", "mailbox_accounts", ["account_id"]
    )
    op.create_index(
        "ix_mailbox_accounts_connect_state",
        "mailbox_accounts",
        ["connect_state"],
    )

    op.create_table(
        "mailbox_messages",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("mailbox_account_id", sa.BigInteger(), nullable=False),
        sa.Column("provider_message_id", sa.String(length=256), nullable=False),
        sa.Column("tender_id", sa.BigInteger(), nullable=True),
        sa.Column("matched_ref", sa.String(length=256), nullable=True),
        sa.Column("subject", sa.Text(), nullable=True),
        sa.Column("sender", sa.String(length=512), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("body_excerpt", sa.Text(), nullable=True),
        sa.Column(
            "attachment_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("links", postgresql.JSONB(), nullable=True),
        sa.Column("draft_reply", sa.Text(), nullable=True),
        sa.Column("draft_status", sa.String(length=16), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["mailbox_account_id"],
            ["mailbox_accounts.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tender_id"], ["tenders.id"], ondelete="SET NULL"
        ),
        sa.UniqueConstraint(
            "mailbox_account_id",
            "provider_message_id",
            name="uq_mailbox_message_provider_id",
        ),
    )
    op.create_index(
        "ix_mailbox_messages_mailbox_account_id",
        "mailbox_messages",
        ["mailbox_account_id"],
    )
    op.create_index(
        "ix_mailbox_messages_tender_id", "mailbox_messages", ["tender_id"]
    )


def downgrade() -> None:
    op.drop_index(
        "ix_mailbox_messages_tender_id", table_name="mailbox_messages"
    )
    op.drop_index(
        "ix_mailbox_messages_mailbox_account_id",
        table_name="mailbox_messages",
    )
    op.drop_table("mailbox_messages")
    op.drop_index(
        "ix_mailbox_accounts_connect_state", table_name="mailbox_accounts"
    )
    op.drop_index(
        "ix_mailbox_accounts_account_id", table_name="mailbox_accounts"
    )
    op.drop_table("mailbox_accounts")
