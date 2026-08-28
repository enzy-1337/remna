"""Fraud detection core: incidents, per-user watch/baseline state, per-detector rollout stage."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0038_fraud_core_tables"
down_revision = "0037_device_history_hwid_ts_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "fraud_incidents",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("subscription_id", sa.Integer(), nullable=True),
        sa.Column("detector", sa.String(length=32), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("stage_at_detection", sa.String(length=16), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=True),
        sa.Column("action_taken", sa.String(length=24), server_default="none", nullable=False),
        sa.Column("status", sa.String(length=16), server_default="open", nullable=False),
        sa.Column("resolved_by", sa.String(length=64), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("topic_message_id", sa.BigInteger(), nullable=True),
        sa.Column("event_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["subscription_id"], ["subscriptions.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_fraud_incidents_user_id", "fraud_incidents", ["user_id"])
    op.create_index("ix_fraud_incidents_detector", "fraud_incidents", ["detector"])
    op.create_index("ix_fraud_incidents_status", "fraud_incidents", ["status"])
    op.create_index("ix_fraud_incidents_event_ts", "fraud_incidents", ["event_ts"])
    op.create_index(
        "ix_fraud_incidents_user_detector_ts",
        "fraud_incidents",
        ["user_id", "detector", "event_ts"],
    )
    op.create_index(
        "ix_fraud_incidents_status_detector",
        "fraud_incidents",
        ["status", "detector"],
    )

    op.create_table(
        "user_fraud_state",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("is_watched", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("watched_reason", sa.Text(), nullable=True),
        sa.Column("watched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("baseline", sa.JSON(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
    )

    op.create_table(
        "fraud_detector_state",
        sa.Column("detector", sa.String(length=32), nullable=False),
        sa.Column("stage", sa.String(length=16), server_default="learning", nullable=False),
        sa.Column("stage_started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("thresholds", sa.JSON(), server_default="{}", nullable=False),
        sa.Column("auto_block_confidence_threshold", sa.Float(), server_default="0.9", nullable=False),
        sa.Column("approved_by", sa.String(length=64), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("detector"),
    )
    op.bulk_insert(
        sa.table(
            "fraud_detector_state",
            sa.column("detector", sa.String()),
        ),
        [
            {"detector": "ip_hop"},
            {"detector": "hwid_collision"},
            {"detector": "traffic_spike"},
        ],
    )


def downgrade() -> None:
    op.drop_table("fraud_detector_state")
    op.drop_table("user_fraud_state")
    op.drop_index("ix_fraud_incidents_status_detector", table_name="fraud_incidents")
    op.drop_index("ix_fraud_incidents_user_detector_ts", table_name="fraud_incidents")
    op.drop_index("ix_fraud_incidents_event_ts", table_name="fraud_incidents")
    op.drop_index("ix_fraud_incidents_status", table_name="fraud_incidents")
    op.drop_index("ix_fraud_incidents_detector", table_name="fraud_incidents")
    op.drop_index("ix_fraud_incidents_user_id", table_name="fraud_incidents")
    op.drop_table("fraud_incidents")
