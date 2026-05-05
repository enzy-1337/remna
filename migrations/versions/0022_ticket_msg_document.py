"""ticket_messages: document_file_id + document_file_name."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0022_ticket_msg_document"
down_revision = "0021_downloader_user_topics"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text("ALTER TABLE alembic_version ALTER COLUMN version_num TYPE VARCHAR(128)"))
    op.add_column("ticket_messages", sa.Column("document_file_id", sa.String(length=256), nullable=True))
    op.add_column("ticket_messages", sa.Column("document_file_name", sa.String(length=256), nullable=True))


def downgrade() -> None:
    op.drop_column("ticket_messages", "document_file_name")
    op.drop_column("ticket_messages", "document_file_id")
