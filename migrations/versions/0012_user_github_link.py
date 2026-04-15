"""Связка Telegram-пользователя с GitHub-профилем."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012_user_github_link"
down_revision = "0011_user_balance_floor_rw_suspended_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("github_id", sa.BigInteger(), nullable=True))
    op.add_column("users", sa.Column("github_username", sa.String(length=255), nullable=True))
    op.add_column("users", sa.Column("github_profile_url", sa.String(length=512), nullable=True))
    op.create_index("ix_users_github_id", "users", ["github_id"], unique=True)
    op.create_index("ix_users_github_username", "users", ["github_username"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_users_github_username", table_name="users")
    op.drop_index("ix_users_github_id", table_name="users")
    op.drop_column("users", "github_profile_url")
    op.drop_column("users", "github_username")
    op.drop_column("users", "github_id")
