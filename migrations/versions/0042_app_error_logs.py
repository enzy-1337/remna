"""Persistent copy of ERROR+ logs so the bot can search/view them without server access."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0042_app_error_logs"
down_revision = "0041_fraud_ip_connection_samples"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "app_error_logs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("service", sa.String(length=32), nullable=False),
        sa.Column("logger_name", sa.String(length=255), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("traceback", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_app_error_logs_service", "app_error_logs", ["service"])
    op.create_index("ix_app_error_logs_created_at", "app_error_logs", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_app_error_logs_created_at", table_name="app_error_logs")
    op.drop_index("ix_app_error_logs_service", table_name="app_error_logs")
    op.drop_table("app_error_logs")
