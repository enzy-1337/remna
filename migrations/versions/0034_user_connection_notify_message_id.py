"""users: connection_notify_message_id — ID сообщения-уведомления о неподключённом устройстве."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0034_user_connection_notify_message_id"
down_revision = "0033_user_connection_notify_sent_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("connection_notify_message_id", sa.BigInteger(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "connection_notify_message_id")
