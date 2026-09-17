"""Публичный сайт (личный кабинет): TOTP-2FA сайта на User + сессии браузера клиента."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0044_site_auth"
down_revision = "0043_notification_log_system_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("site_totp_secret", sa.String(length=64), nullable=True))
    op.add_column(
        "users",
        sa.Column("site_totp_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column("users", sa.Column("site_totp_backup_codes", sa.JSON(), nullable=True))

    op.create_table(
        "user_web_sessions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("session_token", sa.String(length=64), nullable=False),
        sa.Column("fingerprint_hash", sa.String(length=64), nullable=False),
        sa.Column("login_kind", sa.String(length=16), server_default="telegram", nullable=False),
        sa.Column("ip_address", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.String(length=512), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_user_web_sessions_user_id", "user_web_sessions", ["user_id"], unique=False
    )
    op.create_index(
        op.f("ix_user_web_sessions_session_token"),
        "user_web_sessions",
        ["session_token"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_user_web_sessions_session_token"), table_name="user_web_sessions")
    op.drop_index("ix_user_web_sessions_user_id", table_name="user_web_sessions")
    op.drop_table("user_web_sessions")
    op.drop_column("users", "site_totp_backup_codes")
    op.drop_column("users", "site_totp_enabled")
    op.drop_column("users", "site_totp_secret")
