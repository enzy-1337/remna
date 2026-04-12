"""Таблица billing_traffic_meter: учёт шагов ГБ по опросу Remnawave."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012_billing_traffic_meter"
down_revision = "0011_user_balance_floor_rw_suspended_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "billing_traffic_meter",
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("charged_gb_steps", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP")),
    )


def downgrade() -> None:
    op.drop_table("billing_traffic_meter")
