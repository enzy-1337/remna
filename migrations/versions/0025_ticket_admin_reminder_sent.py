"""Одно напоминание админам о тикете без ответа."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0025_ticket_admin_reminder_sent"
down_revision = "0024_subscription_devices_15_to_2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tickets",
        sa.Column("admin_reminder_sent_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tickets", "admin_reminder_sent_at")
