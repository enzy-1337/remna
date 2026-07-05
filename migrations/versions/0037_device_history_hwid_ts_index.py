"""device_history: composite index for global latest-per-hwid admin query."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0037_device_history_hwid_ts_index"
down_revision = "0036_ticket_msg_voice_vidnote"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_device_history_hwid_ts_desc",
        "device_history",
        ["device_hwid", sa.text("event_ts DESC"), sa.text("id DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_device_history_hwid_ts_desc", table_name="device_history")
