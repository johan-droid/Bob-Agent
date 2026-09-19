"""Add user_credentials table for envelope-encrypted credential vault.

Revision ID: e7f8a9b0c1d2
Revises: d3e4f5a6b7c8
Create Date: 2026-09-19

Phase: Chat-Native Secure Configuration & Credential Architecture.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e7f8a9b0c1d2"
down_revision = "d3e4f5a6b7c8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_credentials",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("user_id", sa.String(length=40), nullable=False),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("encrypted_blob", sa.Text(), nullable=False),
        sa.Column("encrypted_dek", sa.Text(), nullable=False),
        sa.Column(
            "encryption_algorithm",
            sa.String(length=30),
            nullable=False,
            server_default="AES-256-GCM-ENVELOPE",
        ),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="healthy"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_validated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_user_credentials_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_credentials")),
        sa.UniqueConstraint(
            "user_id", "provider", "name", name="uq_user_credentials_user_provider_name"
        ),
    )
    op.create_index(
        op.f("ix_user_credentials_user_id"),
        "user_credentials",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_user_credentials_provider"),
        "user_credentials",
        ["provider"],
        unique=False,
    )
    op.create_index(
        op.f("ix_user_credentials_name"),
        "user_credentials",
        ["name"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_user_credentials_name"), table_name="user_credentials")
    op.drop_index(op.f("ix_user_credentials_provider"), table_name="user_credentials")
    op.drop_index(op.f("ix_user_credentials_user_id"), table_name="user_credentials")
    op.drop_table("user_credentials")
