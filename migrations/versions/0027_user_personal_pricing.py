"""Персональная цена тарифа и фиксированная скидка на пользователя."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0027_user_personal_pricing"
down_revision = "0026_promo_activation_eligibility"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("personal_tariff_discount_percent", sa.Numeric(5, 2), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("custom_subscription_month_price_rub", sa.Numeric(12, 2), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "custom_subscription_month_price_rub")
    op.drop_column("users", "personal_tariff_discount_percent")
