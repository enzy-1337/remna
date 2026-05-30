"""RBAC веб-админки: роли и администраторы."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0031_flux_admin_rbac"
down_revision = "0030_flux_family_and_transfers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "admin_roles",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("permissions", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_admin_roles_name"),
    )

    op.create_table(
        "admin_users",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("role_id", sa.Integer(), nullable=True),
        sa.Column("extra_permissions", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("is_superadmin", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["role_id"], ["admin_roles.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", name="uq_admin_users_user_id"),
    )
    op.create_index("ix_admin_users_user_id", "admin_users", ["user_id"], unique=False)
    op.create_index("ix_admin_users_role_id", "admin_users", ["role_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_admin_users_role_id", table_name="admin_users")
    op.drop_index("ix_admin_users_user_id", table_name="admin_users")
    op.drop_table("admin_users")
    op.drop_table("admin_roles")
