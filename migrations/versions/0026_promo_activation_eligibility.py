"""Условия активации промокода: без активной подписки, пауза после покупки."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0026_promo_activation_eligibility"
down_revision = "0025_ticket_admin_reminder_sent"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "promo_codes",
        sa.Column(
            "require_no_active_subscription",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "promo_codes",
        sa.Column("require_no_paid_subscription_months", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("promo_codes", "require_no_paid_subscription_months")
    op.drop_column("promo_codes", "require_no_active_subscription")
