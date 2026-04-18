"""scheduled_broadcasts: цели отправки (пользователи / канал)."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0019_scheduled_broadcast_targets"
down_revision = "0018_broadcast_history"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "scheduled_broadcasts",
        sa.Column("send_to_users", sa.Boolean(), nullable=False, server_default="true"),
    )
    op.add_column(
        "scheduled_broadcasts",
        sa.Column("send_to_channel", sa.Boolean(), nullable=False, server_default="false"),
    )


def downgrade() -> None:
    op.drop_column("scheduled_broadcasts", "send_to_channel")
    op.drop_column("scheduled_broadcasts", "send_to_users")
