"""portal_credentials — cloud-safe encrypted credentials store

Revision ID: 0017
Revises: 0016
Create Date: 2026-06-10

Moves portal logins from the standalone SQLite store (whose Fernet key lived
in the OS keyring) into the main Postgres DB. The Azure App Service container
has no OS keyring and no persistent local disk, so the old design could not
store credentials on the cloud at all. The payload is Fernet-encrypted with
the app master key (CREDENTIALS_ENCRYPTION_KEY application setting) before it
is written; `secret_ciphertext` is never logged or returned by the API.

No data backfill: the deployed app never managed to store anything (the
keyring error fired first), and a local dev re-adds logins via
POST /credentials.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: Union[str, None] = "0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "portal_credentials",
        sa.Column("portal_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("platform_slug", sa.String(length=64), nullable=True),
        sa.Column("secret_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column(
            "valid", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "last_validated_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("portal_id", "user_id"),
    )
    op.create_index(
        "ix_portal_credentials_user_id", "portal_credentials", ["user_id"]
    )


def downgrade() -> None:
    op.drop_index(
        "ix_portal_credentials_user_id", table_name="portal_credentials"
    )
    op.drop_table("portal_credentials")
