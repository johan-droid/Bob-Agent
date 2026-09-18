"""Telegram Gateway E2E: gateway state + outbound task linkage (additive).

Revision ID: d3e4f5a6b7c8
Revises: c2d3e4f5a6b7

Additive only — no existing table is altered destructively.

- ``delivery_outbox.task_id`` / ``reply_to_message_id``: outbound messages
  are linked to the task that produced them and the inbound message they
  answer, so undelivered responses survive restarts and stay attributable.
- ``telegram_gateway_messages``: first-class gateway state per inbound
  update (update -> session -> task linkage, idempotent task creation).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d3e4f5a6b7c8"
down_revision = "c2d3e4f5a6b7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "delivery_outbox", sa.Column("task_id", sa.String(length=40), nullable=True)
    )
    op.create_index(
        op.f("ix_delivery_outbox_task_id"), "delivery_outbox", ["task_id"], unique=False
    )
    op.add_column(
        "delivery_outbox",
        sa.Column("reply_to_message_id", sa.BigInteger(), nullable=True),
    )
    op.create_table(
        "telegram_gateway_messages",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("telegram_update_id", sa.BigInteger(), nullable=True),
        sa.Column("chat_id", sa.String(length=64), nullable=True),
        sa.Column("user_id", sa.String(length=64), nullable=True),
        sa.Column("message_id", sa.BigInteger(), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("session_id", sa.String(length=40), nullable=True),
        sa.Column("task_id", sa.String(length=40), nullable=True),
        sa.Column(
            "processing_status", sa.String(length=16), nullable=False, server_default="RECEIVED"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_telegram_gateway_messages")),
        sa.UniqueConstraint("telegram_update_id", name="uq_tgm_update_id"),
    )
    op.create_index(
        op.f("ix_telegram_gateway_messages_telegram_update_id"),
        "telegram_gateway_messages",
        ["telegram_update_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_telegram_gateway_messages_chat_id"),
        "telegram_gateway_messages",
        ["chat_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_telegram_gateway_messages_session_id"),
        "telegram_gateway_messages",
        ["session_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_telegram_gateway_messages_task_id"),
        "telegram_gateway_messages",
        ["task_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_telegram_gateway_messages_task_id"), table_name="telegram_gateway_messages"
    )
    op.drop_index(
        op.f("ix_telegram_gateway_messages_session_id"), table_name="telegram_gateway_messages"
    )
    op.drop_index(
        op.f("ix_telegram_gateway_messages_chat_id"), table_name="telegram_gateway_messages"
    )
    op.drop_index(
        op.f("ix_telegram_gateway_messages_telegram_update_id"),
        table_name="telegram_gateway_messages",
    )
    op.drop_table("telegram_gateway_messages")
    op.drop_index(op.f("ix_delivery_outbox_task_id"), table_name="delivery_outbox")
    op.drop_column("delivery_outbox", "reply_to_message_id")
    op.drop_column("delivery_outbox", "task_id")
