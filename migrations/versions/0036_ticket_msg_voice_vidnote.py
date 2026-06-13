"""ticket_messages: voice_file_id, video_note_file_id, audio_file_id columns."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0036_ticket_msg_voice_vidnote"
down_revision = "0035_broadcast_media"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ticket_messages", sa.Column("voice_file_id", sa.String(256), nullable=True))
    op.add_column("ticket_messages", sa.Column("video_note_file_id", sa.String(256), nullable=True))
    op.add_column("ticket_messages", sa.Column("audio_file_id", sa.String(256), nullable=True))
    op.add_column("ticket_messages", sa.Column("audio_file_name", sa.String(256), nullable=True))


def downgrade() -> None:
    op.drop_column("ticket_messages", "audio_file_name")
    op.drop_column("ticket_messages", "audio_file_id")
    op.drop_column("ticket_messages", "video_note_file_id")
    op.drop_column("ticket_messages", "voice_file_id")
