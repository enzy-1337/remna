"""Нормализация legacy-лимита: 15 слотов устройств → 2."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0024_subscription_devices_15_to_2"
down_revision = "0023_promo_code_allowed_users"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text("UPDATE subscriptions SET devices_count = 2 WHERE devices_count = 15"))


def downgrade() -> None:
    # Данные: откат не восстанавливает прежние значения (могли быть осознанно изменены).
    pass
