"""Таблица billing_cron_checkpoints для ночного списания за устройства."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009_billing_cron_checkpoints"
down_revision = "0008_user_bot_message_ids"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "billing_cron_checkpoints",
        sa.Column("job_name", sa.String(length=64), nullable=False),
        sa.Column("last_completed_day", sa.Date(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("job_name", name="pk_billing_cron_checkpoints"),
    )


def downgrade() -> None:
    op.drop_table("billing_cron_checkpoints")
