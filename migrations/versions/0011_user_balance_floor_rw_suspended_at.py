"""Метка времени: доступ в Remnawave снят из‑за пола баланса (гибрид v2)."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011_user_balance_floor_rw_suspended_at"
down_revision = "0010_user_optimized_route"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("balance_floor_rw_suspended_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "balance_floor_rw_suspended_at")
