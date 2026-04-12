"""Флаг «оптимизированный маршрут» (squad + надбавка за ГБ в pay-as-you-go)."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010_user_optimized_route"
down_revision = "0009_billing_cron_checkpoints"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "optimized_route_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "optimized_route_enabled")
