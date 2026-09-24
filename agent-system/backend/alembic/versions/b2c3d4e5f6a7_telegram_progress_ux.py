"""Telegram realtime progress UX: outbox edit support.

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-09-23

Additive only:

- ``delivery_outbox.edit_message_id``: the user-visible Telegram message id
  this row must EDIT in place (``editMessageText``) instead of sending a new
  message — this is what makes one stable progress message possible.
- ``delivery_outbox.telegram_message_id``: the Telegram message id captured
  from a successful ``sendMessage`` response, so the stage that created the
  progress message can hand the editable id to every later stage.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b2c3d4e5f6a7"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "delivery_outbox",
        sa.Column("edit_message_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "delivery_outbox",
        sa.Column("telegram_message_id", sa.BigInteger(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("delivery_outbox", "telegram_message_id")
    op.drop_column("delivery_outbox", "edit_message_id")
