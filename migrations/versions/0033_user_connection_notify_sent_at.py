"""users: connection_notify_sent_at — когда отправили уведомление о неподключённом устройстве."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0033_user_connection_notify_sent_at"
down_revision = "0032_flux_tickets_operator_rating"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("connection_notify_sent_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "connection_notify_sent_at")
