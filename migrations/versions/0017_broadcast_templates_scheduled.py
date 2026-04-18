"""Шаблоны рассылки и отложенная отправка."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0017_broadcast_templates_scheduled"
down_revision = "0016_user_web_admin_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "broadcast_templates",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("title", sa.String(length=160), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "scheduled_broadcasts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("body_text", sa.Text(), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("error_text", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_scheduled_broadcasts_scheduled_at",
        "scheduled_broadcasts",
        ["scheduled_at"],
        unique=False,
    )
    op.create_index(
        "ix_scheduled_broadcasts_status",
        "scheduled_broadcasts",
        ["status"],
        unique=False,
    )
    op.execute(
        sa.text(
            "INSERT INTO broadcast_templates (title, body, sort_order) "
            "VALUES (:title, :body, :sort)"
        ).bindparams(
            title="Приветствие",
            body="👋 **Привет!**",
            sort=0,
        )
    )


def downgrade() -> None:
    op.drop_index("ix_scheduled_broadcasts_status", table_name="scheduled_broadcasts")
    op.drop_index("ix_scheduled_broadcasts_scheduled_at", table_name="scheduled_broadcasts")
    op.drop_table("scheduled_broadcasts")
    op.drop_table("broadcast_templates")
