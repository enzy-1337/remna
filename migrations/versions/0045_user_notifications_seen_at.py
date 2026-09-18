"""users.notifications_seen_at — отметка «просмотрено» для колокольчика уведомлений на сайте."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0045_user_notifications_seen_at"
down_revision = "0044_site_auth"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("notifications_seen_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "notifications_seen_at")
