"""Telegram identity + durable transport (multi-user cloud runtime).

Revision ID: f8a9b0c1d2e3
Revises: e6f7a8b9c0d1
Create Date: 2026-09-17

Phase: Telegram-first cloud hardening.

What this migration adds
------------------------

1. ``users`` / ``telegram_accounts`` — the identity chain
   ``Telegram User -> TelegramAccount -> Bob User -> Role``. Telegram identity
   is never authorization; the account's role is what a principal may do.
   ``telegram_user_id`` is UNIQUE so the mapping is a function.
2. ``telegram_updates`` — the ingest ledger keyed by Telegram ``update_id``.
   The UNIQUE primary key makes webhook/poll redelivery idempotent: the first
   insert wins, the loser re-processes the winner's stored payload. The row
   doubles as the durable payload buffer, so a worker can claim and execute
   updates after a web-dyno restart.
3. ``delivery_outbox`` — durable outbound Telegram notifications. State is
   persisted BEFORE delivery; delivery loops claim atomically, back off
   exponentially, dead-letter after ``max_attempts``, and reap stuck claims.
4. Ownership columns — ``sessions.owner_user_id``, ``tasks.owner_user_id``,
   ``approvals.owner_user_id``, ``memory_notes.owner_user_id``. NULL is
   backward compatible (legacy/local rows are operator-owned); the ownership
   layer treats NULL + local mode as the operator (services/identity.py).

Deploy read-rule: migrate, deploy code, then flip ``AGENT_IDENTITY_MODE``.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f8a9b0c1d2e3"
down_revision = "e6f7a8b9c0d1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("auth_provider", sa.String(length=20), nullable=False, server_default="telegram"),
        sa.Column("role", sa.String(length=20), nullable=False, server_default="member"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
    )

    op.create_table(
        "telegram_accounts",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("telegram_user_id", sa.String(length=40), nullable=False),
        sa.Column("user_id", sa.String(length=40), nullable=False),
        sa.Column("chat_id", sa.String(length=40), nullable=True),
        sa.Column("role", sa.String(length=20), nullable=False, server_default="member"),
        sa.Column("linked_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_telegram_accounts_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_telegram_accounts")),
        sa.UniqueConstraint("telegram_user_id", name="uq_telegram_accounts_tg_user_id"),
    )
    op.create_index(
        op.f("ix_telegram_accounts_telegram_user_id"),
        "telegram_accounts",
        ["telegram_user_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_telegram_accounts_user_id"), "telegram_accounts", ["user_id"], unique=False
    )

    op.create_table(
        "telegram_updates",
        sa.Column("update_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("account_id", sa.String(length=40), nullable=True),
        sa.Column("chat_id", sa.String(length=40), nullable=True),
        sa.Column("payload_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("update_id", name=op.f("pk_telegram_updates")),
    )

    op.create_table(
        "delivery_outbox",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("channel", sa.String(length=20), nullable=False, server_default="telegram"),
        sa.Column("kind", sa.String(length=30), nullable=False, server_default="notification"),
        sa.Column("chat_id", sa.String(length=40), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("reply_markup_json", sa.JSON(), nullable=True),
        sa.Column("event_id", sa.String(length=40), nullable=True),
        sa.Column("state", sa.String(length=12), nullable=False, server_default="PENDING"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=True,
            server_default=sa.func.now(),
        ),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_by", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_delivery_outbox")),
    )
    op.create_index(
        op.f("ix_delivery_outbox_channel"), "delivery_outbox", ["channel"], unique=False
    )
    op.create_index(op.f("ix_delivery_outbox_kind"), "delivery_outbox", ["kind"], unique=False)
    op.create_index(op.f("ix_delivery_outbox_state"), "delivery_outbox", ["state"], unique=False)
    op.create_index(
        op.f("ix_delivery_outbox_next_attempt_at"),
        "delivery_outbox",
        ["next_attempt_at"],
        unique=False,
    )
    op.create_index(op.f("ix_delivery_outbox_event_id"), "delivery_outbox", ["event_id"], unique=False)

    for table in ("sessions", "tasks", "approvals", "memory_notes"):
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.add_column(sa.Column("owner_user_id", sa.String(length=40), nullable=True))
            batch_op.create_index(
                op.f(f"ix_{table}_owner_user_id"), ["owner_user_id"], unique=False
            )


def downgrade() -> None:
    for table in ("memory_notes", "approvals", "tasks", "sessions"):
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.drop_index(op.f(f"ix_{table}_owner_user_id"))
            batch_op.drop_column("owner_user_id")

    op.drop_index(
        op.f("ix_delivery_outbox_event_id"), table_name="delivery_outbox"
    )
    op.drop_index(
        op.f("ix_delivery_outbox_next_attempt_at"), table_name="delivery_outbox"
    )
    op.drop_index(op.f("ix_delivery_outbox_kind"), table_name="delivery_outbox")
    op.drop_index(op.f("ix_delivery_outbox_state"), table_name="delivery_outbox")
    op.drop_index(op.f("ix_delivery_outbox_channel"), table_name="delivery_outbox")
    op.drop_table("delivery_outbox")
    op.drop_table("telegram_updates")
    op.drop_index(op.f("ix_telegram_accounts_user_id"), table_name="telegram_accounts")
    op.drop_index(
        op.f("ix_telegram_accounts_telegram_user_id"), table_name="telegram_accounts"
    )
    op.drop_table("telegram_accounts")
    op.drop_table("users")
