"""Тарифы: скидка за длинный срок — 2 месяца −5% (170 ₽/мес), 3 месяца −12,5% (157 ₽/мес) при базе 179 ₽.

Цена за месяц со скидкой округляется до целых ₽ и умножается на число месяцев (см.
subscription_service.calculate_tariff_price_from_base_month). База — текущая цена тарифа «1 месяц».
"""

from __future__ import annotations

from alembic import op

revision = "0050_plan_tiered_discounts"
down_revision = "0049_user_totp_codes_jsonb"
branch_labels = None
depends_on = None

_RECALC = """
UPDATE plans p
   SET price_rub = ROUND(ref.price_rub * (1 - p.discount_percent / 100)) * (p.duration_days / 30)
  FROM (SELECT price_rub FROM plans WHERE name = '1 месяц' ORDER BY id LIMIT 1) ref
 WHERE p.name IN ('2 месяца', '3 месяца')
"""


def upgrade() -> None:
    op.execute("UPDATE plans SET discount_percent = 5 WHERE name = '2 месяца'")
    op.execute("UPDATE plans SET discount_percent = 12.5 WHERE name = '3 месяца'")
    op.execute(_RECALC)


def downgrade() -> None:
    op.execute("UPDATE plans SET discount_percent = 2 WHERE name = '2 месяца'")
    op.execute("UPDATE plans SET discount_percent = 5 WHERE name = '3 месяца'")
    op.execute(_RECALC)
