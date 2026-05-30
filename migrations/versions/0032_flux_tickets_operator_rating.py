"""Тикеты: operator_id, rating_requested; новая схема ticket_ratings."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0032_flux_tickets_operator_rating"
down_revision = "0031_flux_admin_rbac"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tickets",
        sa.Column("rating_requested", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column("tickets", sa.Column("operator_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_tickets_operator_id_admin_users",
        "tickets",
        "admin_users",
        ["operator_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_tickets_operator_id", "tickets", ["operator_id"], unique=False)

    op.execute(
        """
        UPDATE tickets t
        SET operator_id = au.id
        FROM admin_users au
        WHERE t.assigned_admin_id IS NOT NULL
          AND au.user_id = t.assigned_admin_id
        """
    )

    op.drop_constraint("tickets_assigned_admin_id_fkey", "tickets", type_="foreignkey")
    op.drop_column("tickets", "assigned_admin_id")

    op.add_column("ticket_ratings", sa.Column("operator_id", sa.Integer(), nullable=True))
    op.add_column("ticket_ratings", sa.Column("user_id", sa.Integer(), nullable=True))
    op.add_column("ticket_ratings", sa.Column("value", sa.SmallInteger(), nullable=True))

    op.execute(
        """
        UPDATE ticket_ratings tr
        SET value = CASE WHEN tr.rating THEN 1 ELSE -1 END,
            user_id = t.user_id,
            operator_id = t.operator_id
        FROM tickets t
        WHERE tr.ticket_id = t.id
        """
    )

    op.drop_column("ticket_ratings", "rating")

    op.alter_column("ticket_ratings", "user_id", nullable=False)
    op.alter_column("ticket_ratings", "value", nullable=False)

    op.create_foreign_key(
        "fk_ticket_ratings_operator_id_admin_users",
        "ticket_ratings",
        "admin_users",
        ["operator_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_ticket_ratings_user_id_users",
        "ticket_ratings",
        "users",
        ["user_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_ticket_ratings_operator_id", "ticket_ratings", ["operator_id"], unique=False)
    op.create_index("ix_ticket_ratings_user_id", "ticket_ratings", ["user_id"], unique=False)
    op.create_unique_constraint("uq_ticket_ratings_ticket_id", "ticket_ratings", ["ticket_id"])


def downgrade() -> None:
    op.drop_constraint("uq_ticket_ratings_ticket_id", "ticket_ratings", type_="unique")
    op.drop_index("ix_ticket_ratings_user_id", table_name="ticket_ratings")
    op.drop_index("ix_ticket_ratings_operator_id", table_name="ticket_ratings")
    op.drop_constraint("fk_ticket_ratings_user_id_users", "ticket_ratings", type_="foreignkey")
    op.drop_constraint("fk_ticket_ratings_operator_id_admin_users", "ticket_ratings", type_="foreignkey")

    op.add_column("ticket_ratings", sa.Column("rating", sa.Boolean(), nullable=True))
    op.execute(
        """
        UPDATE ticket_ratings
        SET rating = (value >= 1)
        WHERE value IS NOT NULL
        """
    )
    op.alter_column("ticket_ratings", "rating", nullable=False)
    op.drop_column("ticket_ratings", "value")
    op.drop_column("ticket_ratings", "user_id")
    op.drop_column("ticket_ratings", "operator_id")

    op.add_column("tickets", sa.Column("assigned_admin_id", sa.Integer(), nullable=True))
    op.execute(
        """
        UPDATE tickets t
        SET assigned_admin_id = au.user_id
        FROM admin_users au
        WHERE t.operator_id IS NOT NULL
          AND au.id = t.operator_id
        """
    )
    op.create_foreign_key(
        "tickets_assigned_admin_id_fkey",
        "tickets",
        "users",
        ["assigned_admin_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.drop_index("ix_tickets_operator_id", table_name="tickets")
    op.drop_constraint("fk_tickets_operator_id_admin_users", "tickets", type_="foreignkey")
    op.drop_column("tickets", "operator_id")
    op.drop_column("tickets", "rating_requested")
