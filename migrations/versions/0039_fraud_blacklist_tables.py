"""Fraud detection: external Telegram-ID blacklist entries + sync state."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0039_fraud_blacklist_tables"
down_revision = "0038_fraud_core_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "telegram_blacklist_entries",
        sa.Column("telegram_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("source", sa.String(length=32), server_default="bedolaga_dev", nullable=False),
        sa.PrimaryKeyConstraint("telegram_id"),
    )

    op.create_table(
        "telegram_blacklist_sync_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("etag", sa.String(length=255), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_id_count", sa.Integer(), server_default="0", nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.bulk_insert(
        sa.table("telegram_blacklist_sync_state", sa.column("id", sa.Integer())),
        [{"id": 1}],
    )


def downgrade() -> None:
    op.drop_table("telegram_blacklist_sync_state")
    op.drop_table("telegram_blacklist_entries")
