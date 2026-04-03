"""Добавление photo_file_id для вложений в сообщениях тикетов."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision ≤ 32 символа — колонка alembic_version.version_num (VARCHAR(32))
revision = "0005_ticket_msg_photo"
down_revision = "0004_tickets_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ticket_messages",
        sa.Column("photo_file_id", sa.String(length=256), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("ticket_messages", "photo_file_id")
