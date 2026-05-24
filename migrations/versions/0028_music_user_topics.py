"""Forum topics для music-бота."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0028_music_user_topics"
down_revision = "0027_user_personal_pricing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "music_user_topics",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("forum_chat_id", sa.BigInteger(), nullable=False),
        sa.Column("topic_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("forum_chat_id", "topic_id", name="uq_music_forum_topic"),
    )
    op.create_index("ix_music_user_topics_telegram_id", "music_user_topics", ["telegram_id"], unique=True)
    op.create_index("ix_music_user_topics_forum_chat_id", "music_user_topics", ["forum_chat_id"], unique=False)
    op.create_index("ix_music_user_topics_user_id", "music_user_topics", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_music_user_topics_user_id", table_name="music_user_topics")
    op.drop_index("ix_music_user_topics_forum_chat_id", table_name="music_user_topics")
    op.drop_index("ix_music_user_topics_telegram_id", table_name="music_user_topics")
    op.drop_table("music_user_topics")
