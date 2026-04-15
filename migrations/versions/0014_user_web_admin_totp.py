"""Web-admin TOTP 2FA fields on users."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014_user_web_admin_totp"
down_revision = "0013_merge_heads_github_and_meter"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("web_admin_totp_secret", sa.String(length=64), nullable=True))
    op.add_column(
        "users",
        sa.Column("web_admin_totp_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )


def downgrade() -> None:
    op.drop_column("users", "web_admin_totp_enabled")
    op.drop_column("users", "web_admin_totp_secret")
