"""Persist web-admin sessions on users."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0016_user_web_admin_sessions"
down_revision = "0015_user_low_balance_notified_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("web_admin_session_token", sa.String(length=64), nullable=True))
    op.add_column("users", sa.Column("web_admin_session_expires_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "web_admin_session_expires_at")
    op.drop_column("users", "web_admin_session_token")
