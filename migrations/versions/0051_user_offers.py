"""users: поля акций — intro «2 недели за 1 ₽» и скидки «вернись» (win-back)."""

from __future__ import annotations

from alembic import op

revision = "0051_user_offers"
down_revision = "0050_plan_tiered_discounts"
branch_labels = None
depends_on = None

_COLS = ("intro_offer_used_at", "winback_offered_at", "winback_offer_until", "winback_offer_used_at")


def upgrade() -> None:
    for c in _COLS:
        op.execute(f"ALTER TABLE users ADD COLUMN IF NOT EXISTS {c} TIMESTAMP WITH TIME ZONE NULL")


def downgrade() -> None:
    for c in _COLS:
        op.drop_column("users", c)
