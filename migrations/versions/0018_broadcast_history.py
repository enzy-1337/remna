"""История массовых рассылок web-admin."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0018_broadcast_history"
down_revision = "0017_broadcast_templates_scheduled"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "broadcast_history",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("body_text", sa.Text(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("recipients_ok", sa.Integer(), server_default="0", nullable=False),
        sa.Column("recipients_failed", sa.Integer(), server_default="0", nullable=False),
        sa.Column("source", sa.String(length=32), server_default="mass", nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_broadcast_history_sent_at", "broadcast_history", ["sent_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_broadcast_history_sent_at", table_name="broadcast_history")
    op.drop_table("broadcast_history")
