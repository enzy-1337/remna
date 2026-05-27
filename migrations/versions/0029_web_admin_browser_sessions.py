"""Browser sessions for web-admin (fingerprint, 24h TTL)."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0029_web_admin_browser_sessions"
down_revision = "0028_music_user_topics"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "web_admin_browser_sessions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("session_token", sa.String(length=64), nullable=False),
        sa.Column("fingerprint_hash", sa.String(length=64), nullable=False),
        sa.Column("login_kind", sa.String(length=16), nullable=False),
        sa.Column("ip_address", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.String(length=512), nullable=True),
        sa.Column("auth_snapshot", sa.JSON(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_web_admin_browser_sessions_user_fp",
        "web_admin_browser_sessions",
        ["user_id", "fingerprint_hash"],
    )
    op.create_index(
        op.f("ix_web_admin_browser_sessions_session_token"),
        "web_admin_browser_sessions",
        ["session_token"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_web_admin_browser_sessions_session_token"), table_name="web_admin_browser_sessions")
    op.drop_index("ix_web_admin_browser_sessions_user_fp", table_name="web_admin_browser_sessions")
    op.drop_table("web_admin_browser_sessions")
