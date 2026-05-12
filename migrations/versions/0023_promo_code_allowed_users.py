"""Список разрешённых пользователей для промокода (опциональный allow-list)."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0023_promo_code_allowed_users"
down_revision = "0022_ticket_msg_document"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "promo_code_allowed_users",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("promo_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["promo_id"], ["promo_codes.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "promo_id", "user_id", name="uq_promo_code_allowed_user"
        ),
    )
    op.create_index(
        "ix_promo_code_allowed_users_promo_id",
        "promo_code_allowed_users",
        ["promo_id"],
        unique=False,
    )
    op.create_index(
        "ix_promo_code_allowed_users_user_id",
        "promo_code_allowed_users",
        ["user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_promo_code_allowed_users_user_id",
        table_name="promo_code_allowed_users",
    )
    op.drop_index(
        "ix_promo_code_allowed_users_promo_id",
        table_name="promo_code_allowed_users",
    )
    op.drop_table("promo_code_allowed_users")
