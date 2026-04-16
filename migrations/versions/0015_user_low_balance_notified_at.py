"""Persist low-balance notification state on users."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015_user_low_balance_notified_at"
down_revision = "0014_user_web_admin_totp"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("low_balance_notified_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "low_balance_notified_at")
