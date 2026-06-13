"""broadcast: media_type and media_file_id for photo/document broadcasts."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0035_broadcast_media"
down_revision = "0034_user_connection_notify_message_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("scheduled_broadcasts", sa.Column("media_type", sa.String(16), nullable=True))
    op.add_column("scheduled_broadcasts", sa.Column("media_file_id", sa.Text(), nullable=True))
    op.add_column("broadcast_history", sa.Column("media_type", sa.String(16), nullable=True))
    op.add_column("broadcast_history", sa.Column("media_file_id", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("scheduled_broadcasts", "media_file_id")
    op.drop_column("scheduled_broadcasts", "media_type")
    op.drop_column("broadcast_history", "media_file_id")
    op.drop_column("broadcast_history", "media_type")
