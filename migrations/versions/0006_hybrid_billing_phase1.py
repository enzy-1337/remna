"""Hybrid billing phase 1 schema."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_hybrid_billing_phase1"
down_revision = "0005_ticket_msg_photo"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text("ALTER TABLE alembic_version ALTER COLUMN version_num TYPE VARCHAR(128)"))

    op.add_column("users", sa.Column("billing_mode", sa.String(length=16), nullable=False, server_default="legacy"))
    op.add_column("users", sa.Column("lifetime_exempt_flag", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("users", sa.Column("risk_notified_24h_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("risk_notified_1h_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_users_billing_mode", "users", ["billing_mode"], unique=False)

    op.add_column("plans", sa.Column("device_limit", sa.Integer(), nullable=True))
    op.add_column("plans", sa.Column("monthly_gb_limit", sa.Integer(), nullable=True))
    op.add_column("plans", sa.Column("is_package_monthly", sa.Boolean(), nullable=False, server_default=sa.false()))

    op.create_table(
        "billing_usage_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_id", sa.String(length=128), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("event_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("usage_gb_step", sa.Integer(), nullable=True),
        sa.Column("device_hwid", sa.String(length=255), nullable=True),
        sa.Column("is_mobile_internet", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("meta", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("event_id", name="uq_billing_usage_events_event_id"),
    )
    op.create_index("ix_billing_usage_events_user_ts", "billing_usage_events", ["user_id", "event_ts"], unique=False)

    op.create_table(
        "billing_ledger_entries",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("entry_type", sa.String(length=32), nullable=False),
        sa.Column("amount_rub", sa.Numeric(12, 2), nullable=False),
        sa.Column("balance_after_rub", sa.Numeric(12, 2), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("source_ref", sa.String(length=255), nullable=True),
        sa.Column("meta", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("idempotency_key", name="uq_billing_ledger_entries_idempotency_key"),
    )
    op.create_index("ix_billing_ledger_entries_user_created", "billing_ledger_entries", ["user_id", "created_at"], unique=False)

    op.create_table(
        "billing_daily_summary",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("gb_units", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("device_units", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("mobile_gb_units", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("gb_amount_rub", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("device_amount_rub", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("mobile_amount_rub", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("total_amount_rub", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "day", name="uq_billing_daily_summary_user_day"),
    )

    op.create_table(
        "device_history",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("subscription_id", sa.Integer(), sa.ForeignKey("subscriptions.id", ondelete="SET NULL"), nullable=True),
        sa.Column("device_hwid", sa.String(length=255), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("event_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("meta", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_device_history_user_ts", "device_history", ["user_id", "event_ts"], unique=False)

    op.create_table(
        "remnawave_webhook_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.String(length=128), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="received"),
        sa.Column("signature_valid", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("headers", sa.JSON(), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.UniqueConstraint("event_id", name="uq_remnawave_webhook_events_event_id"),
    )


def downgrade() -> None:
    op.drop_table("remnawave_webhook_events")
    op.drop_index("ix_device_history_user_ts", table_name="device_history")
    op.drop_table("device_history")
    op.drop_table("billing_daily_summary")
    op.drop_index("ix_billing_ledger_entries_user_created", table_name="billing_ledger_entries")
    op.drop_table("billing_ledger_entries")
    op.drop_index("ix_billing_usage_events_user_ts", table_name="billing_usage_events")
    op.drop_table("billing_usage_events")

    op.drop_column("plans", "is_package_monthly")
    op.drop_column("plans", "monthly_gb_limit")
    op.drop_column("plans", "device_limit")

    op.drop_index("ix_users_billing_mode", table_name="users")
    op.drop_column("users", "risk_notified_1h_at")
    op.drop_column("users", "risk_notified_24h_at")
    op.drop_column("users", "lifetime_exempt_flag")
    op.drop_column("users", "billing_mode")
