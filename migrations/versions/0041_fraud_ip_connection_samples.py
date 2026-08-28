"""Fraud detection: raw IP-connection samples for the ip_hop detector."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0041_fraud_ip_connection_samples"
down_revision = "0040_fraud_traffic_samples"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ip_connection_samples",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("ip", sa.String(length=64), nullable=False),
        sa.Column("node_uuid", sa.String(length=64), nullable=True),
        sa.Column("asn", sa.Integer(), nullable=True),
        sa.Column("asn_org", sa.String(length=255), nullable=True),
        sa.Column("seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ip_connection_samples_user_id", "ip_connection_samples", ["user_id"])
    op.create_index("ix_ip_connection_samples_seen_at", "ip_connection_samples", ["seen_at"])
    op.create_index(
        "ix_ip_connection_samples_user_seen_desc",
        "ip_connection_samples",
        ["user_id", "seen_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_ip_connection_samples_user_seen_desc", table_name="ip_connection_samples")
    op.drop_index("ix_ip_connection_samples_seen_at", table_name="ip_connection_samples")
    op.drop_index("ix_ip_connection_samples_user_id", table_name="ip_connection_samples")
    op.drop_table("ip_connection_samples")
