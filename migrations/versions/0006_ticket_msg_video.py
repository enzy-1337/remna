"""Добавление video_file_id для вложений в сообщениях тикетов."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_ticket_msg_video"
down_revision = "0005_ticket_msg_photo"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text("ALTER TABLE alembic_version ALTER COLUMN version_num TYPE VARCHAR(128)"))
    op.add_column(
        "ticket_messages",
        sa.Column("video_file_id", sa.String(length=256), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("ticket_messages", "video_file_id")
