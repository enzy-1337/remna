"""Уникальность одной награды first_paid_plan на приглашённого (PostgreSQL partial index).

Revision ID: 0002_referral_uq
Revises: 0001_initial
Create Date: 2025-03-19

"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0002_referral_uq"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "uq_referral_rewards_referred_first_paid",
        "referral_rewards",
        ["referred_id"],
        unique=True,
        postgresql_where=sa.text("source = 'first_paid_plan'"),
    )


def downgrade() -> None:
    op.drop_index("uq_referral_rewards_referred_first_paid", table_name="referral_rewards")
