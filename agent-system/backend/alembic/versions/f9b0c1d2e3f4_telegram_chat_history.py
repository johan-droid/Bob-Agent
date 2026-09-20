"""Add telegram_chat_history table for Telegram multi-turn chat context.

Revision ID: f9b0c1d2e3f4
Revises: e7f8a9b0c1d2
Create Date: 2026-09-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f9b0c1d2e3f4"
down_revision = "e7f8a9b0c1d2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "telegram_chat_history",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("chat_id", sa.String(length=64), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_telegram_chat_history")),
    )
    op.create_index(
        op.f("ix_telegram_chat_history_chat_id"),
        "telegram_chat_history",
        ["chat_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_telegram_chat_history_created_at"),
        "telegram_chat_history",
        ["created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_telegram_chat_history_created_at"), table_name="telegram_chat_history")
    op.drop_index(op.f("ix_telegram_chat_history_chat_id"), table_name="telegram_chat_history")
    op.drop_table("telegram_chat_history")
