"""notifications_log: user_id nullable (system-wide events with no subject user, e.g.
bot/api startup, backups, daily report) + index on sent_at for the new Logs tab."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0043_notification_log_system_events"
down_revision = "0042_app_error_logs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "notifications_log",
        "user_id",
        existing_type=sa.Integer(),
        nullable=True,
    )
    op.create_index(
        "ix_notifications_log_sent_at", "notifications_log", ["sent_at"], unique=False
    )
    op.create_index(
        "ix_notifications_log_type", "notifications_log", ["type"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_notifications_log_type", table_name="notifications_log")
    op.drop_index("ix_notifications_log_sent_at", table_name="notifications_log")
    op.alter_column(
        "notifications_log",
        "user_id",
        existing_type=sa.Integer(),
        nullable=False,
    )
