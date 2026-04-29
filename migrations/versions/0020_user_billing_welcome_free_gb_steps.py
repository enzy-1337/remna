"""Счётчик бесплатных PAYG-шагов ГБ после welcome при первом пополнении."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0020_user_billing_welcome_free_gb_steps"
down_revision = "0019_scheduled_broadcast_targets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "billing_welcome_free_gb_steps_remaining",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "billing_welcome_free_gb_steps_remaining")
