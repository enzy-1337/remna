"""Merge heads: github link and traffic meter."""

from __future__ import annotations

from alembic import op

revision = "0013_merge_heads_github_and_meter"
down_revision = ("0012_billing_traffic_meter", "0012_user_github_link")
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Merge-only migration: schema changes are in parent heads.
    pass


def downgrade() -> None:
    # No-op for merge revision.
    pass
