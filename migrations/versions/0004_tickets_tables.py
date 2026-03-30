"""Добавление тикет-системы.

Таблицы:
- tickets
- ticket_messages
- ticket_ratings
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0004_tickets_tables"
down_revision = "0003_promo_usage_uq"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tickets",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        # Внутренний id пользователя (users.id)
        sa.Column("user_id", sa.Integer(), nullable=False),
        # Дополнительно храним Telegram user_id (удобно для связки с ботом/форумом).
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="open"),
        sa.Column("topic_id", sa.BigInteger(), nullable=False),
        # Внутренний id администратора (users.id)
        sa.Column("assigned_admin_id", sa.Integer(), nullable=True),
        # Telegram id администратора (опционально)
        sa.Column("telegram_assigned_admin_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_activity", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["assigned_admin_id"], ["users.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_tickets_user_id", "tickets", ["user_id"], unique=False)
    op.create_index("ix_tickets_status", "tickets", ["status"], unique=False)
    op.create_index("ix_tickets_topic_id", "tickets", ["topic_id"], unique=False)

    op.create_table(
        "ticket_messages",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("ticket_id", sa.Integer(), nullable=False),
        # Внутренний id отправителя (users.id) если это пользователь/админ из БД.
        sa.Column("sender_id", sa.Integer(), nullable=True),
        sa.Column("sender_role", sa.String(length=16), nullable=False),
        sa.Column("sender_telegram_id", sa.BigInteger(), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("is_internal", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.ForeignKeyConstraint(["ticket_id"], ["tickets.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["sender_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ticket_messages_ticket_id", "ticket_messages", ["ticket_id"], unique=False)
    op.create_index("ix_ticket_messages_created_at", "ticket_messages", ["created_at"], unique=False)

    op.create_table(
        "ticket_ratings",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("ticket_id", sa.Integer(), nullable=False),
        sa.Column("rating", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["ticket_id"], ["tickets.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ticket_ratings_ticket_id", "ticket_ratings", ["ticket_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_ticket_ratings_ticket_id", table_name="ticket_ratings")
    op.drop_table("ticket_ratings")

    op.drop_index("ix_ticket_messages_created_at", table_name="ticket_messages")
    op.drop_index("ix_ticket_messages_ticket_id", table_name="ticket_messages")
    op.drop_table("ticket_messages")

    op.drop_index("ix_tickets_topic_id", table_name="tickets")
    op.drop_index("ix_tickets_status", table_name="tickets")
    op.drop_index("ix_tickets_user_id", table_name="tickets")
    op.drop_table("tickets")

