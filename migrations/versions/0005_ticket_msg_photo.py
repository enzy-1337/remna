"""Добавление photo_file_id для вложений в сообщениях тикетов."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# Короткий id: в upgrade() расширяем alembic_version.version_num (по умолчанию VARCHAR(32)).
revision = "0005_ticket_msg_photo"
down_revision = "0004_tickets_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Alembic создаёт version_num как VARCHAR(32) — длинные имена ревизий ломают финальный UPDATE.
    op.execute(sa.text("ALTER TABLE alembic_version ALTER COLUMN version_num TYPE VARCHAR(128)"))
    op.add_column(
        "ticket_messages",
        sa.Column("photo_file_id", sa.String(length=256), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("ticket_messages", "photo_file_id")
