"""users: email/email_verified_at (вход и привязка по почте) + google_id (вход через Google)."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0046_user_email_google"
down_revision = "0045_user_notifications_seen_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("email", sa.String(length=255), nullable=True))
    op.add_column("users", sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("google_id", sa.String(length=64), nullable=True))
    op.create_index("ix_users_email", "users", ["email"], unique=True)
    op.create_index("ix_users_google_id", "users", ["google_id"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_users_google_id", table_name="users")
    op.drop_index("ix_users_email", table_name="users")
    op.drop_column("users", "google_id")
    op.drop_column("users", "email_verified_at")
    op.drop_column("users", "email")
