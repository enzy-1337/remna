"""Таблица привязки downloader-пользователя к forum topic."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0021_downloader_user_topics"
down_revision = "0020_user_billing_welcome_free_gb_steps"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "downloader_user_topics",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("forum_chat_id", sa.BigInteger(), nullable=False),
        sa.Column("topic_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("forum_chat_id", "topic_id", name="uq_downloader_forum_topic"),
    )
    op.create_index(
        "ix_downloader_user_topics_telegram_id",
        "downloader_user_topics",
        ["telegram_id"],
        unique=True,
    )
    op.create_index(
        "ix_downloader_user_topics_forum_chat_id",
        "downloader_user_topics",
        ["forum_chat_id"],
        unique=False,
    )
    op.create_index(
        "ix_downloader_user_topics_user_id",
        "downloader_user_topics",
        ["user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_downloader_user_topics_user_id", table_name="downloader_user_topics")
    op.drop_index("ix_downloader_user_topics_forum_chat_id", table_name="downloader_user_topics")
    op.drop_index("ix_downloader_user_topics_telegram_id", table_name="downloader_user_topics")
    op.drop_table("downloader_user_topics")
