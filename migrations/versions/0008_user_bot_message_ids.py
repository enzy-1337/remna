"""User columns: last Telegram message ids for referral/device replace-notify."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008_user_bot_message_ids"
down_revision = "0007_merge_heads_ticket_and_billing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("referral_bonus_message_id", sa.BigInteger(), nullable=True))
    op.add_column("users", sa.Column("device_notify_message_id", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "device_notify_message_id")
    op.drop_column("users", "referral_bonus_message_id")
