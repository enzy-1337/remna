"""Merge heads: hybrid billing + ticket video."""

from __future__ import annotations

revision = "0007_merge_heads_ticket_and_billing"
down_revision = ("0006_hybrid_billing_phase1", "0006_ticket_msg_video")
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Merge revision: no schema changes.
    pass


def downgrade() -> None:
    # Split heads back: no schema changes.
    pass
