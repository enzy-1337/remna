"""Fraud detection: raw traffic samples for the traffic_spike detector."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0040_fraud_traffic_samples"
down_revision = "0039_fraud_blacklist_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "traffic_samples",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("raw_bytes_total", sa.BigInteger(), nullable=False),
        sa.Column("sampled_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_traffic_samples_user_id", "traffic_samples", ["user_id"])
    op.create_index("ix_traffic_samples_sampled_at", "traffic_samples", ["sampled_at"])


def downgrade() -> None:
    op.drop_index("ix_traffic_samples_sampled_at", table_name="traffic_samples")
    op.drop_index("ix_traffic_samples_user_id", table_name="traffic_samples")
    op.drop_table("traffic_samples")
