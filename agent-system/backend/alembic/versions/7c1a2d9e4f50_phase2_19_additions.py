"""Phase 2-19 additions: task results.

Revision ID: 7c1a2d9e4f50
Revises: 94be8eadb99f
Create Date: 2026-09-06

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "7c1a2d9e4f50"
down_revision = "94be8eadb99f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("result_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("tasks", "result_json")
