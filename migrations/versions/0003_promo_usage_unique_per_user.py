"""Запрет повторного использования одного промокода пользователем.

Revision ID: 0003_promo_usage_uq
Revises: 0002_referral_uq
Create Date: 2025-03-19
"""

from __future__ import annotations

from alembic import op

revision = "0003_promo_usage_uq"
down_revision = "0002_referral_uq"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "uq_promo_usages_promo_user",
        "promo_usages",
        ["promo_id", "user_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_promo_usages_promo_user", table_name="promo_usages")
