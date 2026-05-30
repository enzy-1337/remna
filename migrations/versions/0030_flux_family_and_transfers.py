"""Семейная подписка и журнал передач."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0030_flux_family_and_transfers"
down_revision = "0029_web_admin_browser_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "family_members",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("owner_user_id", sa.Integer(), nullable=False),
        sa.Column("member_user_id", sa.Integer(), nullable=False),
        sa.Column("subscription_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["member_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["subscription_id"], ["subscriptions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("member_user_id", name="uq_family_members_member_user_id"),
    )
    op.create_index("ix_family_members_owner_user_id", "family_members", ["owner_user_id"], unique=False)
    op.create_index("ix_family_members_subscription_id", "family_members", ["subscription_id"], unique=False)

    op.create_table(
        "subscription_transfers",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("from_user_id", sa.Integer(), nullable=False),
        sa.Column("to_user_id", sa.Integer(), nullable=False),
        sa.Column("subscription_id", sa.Integer(), nullable=False),
        sa.Column("transferred_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["from_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["to_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["subscription_id"], ["subscriptions.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_subscription_transfers_from_user_id", "subscription_transfers", ["from_user_id"], unique=False)
    op.create_index("ix_subscription_transfers_to_user_id", "subscription_transfers", ["to_user_id"], unique=False)
    op.create_index("ix_subscription_transfers_subscription_id", "subscription_transfers", ["subscription_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_subscription_transfers_subscription_id", table_name="subscription_transfers")
    op.drop_index("ix_subscription_transfers_to_user_id", table_name="subscription_transfers")
    op.drop_index("ix_subscription_transfers_from_user_id", table_name="subscription_transfers")
    op.drop_table("subscription_transfers")
    op.drop_index("ix_family_members_subscription_id", table_name="family_members")
    op.drop_index("ix_family_members_owner_user_id", table_name="family_members")
    op.drop_table("family_members")
