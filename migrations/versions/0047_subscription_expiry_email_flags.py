"""subscriptions: expiry_email_3d / expiry_email_6h — письма на почту перед окончанием подписки."""

from __future__ import annotations

from alembic import op

revision = "0047_sub_expiry_email_flags"
down_revision = "0046_user_email_google"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS expiry_email_3d boolean NOT NULL DEFAULT false")
    op.execute("ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS expiry_email_6h boolean NOT NULL DEFAULT false")


def downgrade() -> None:
    op.drop_column("subscriptions", "expiry_email_6h")
    op.drop_column("subscriptions", "expiry_email_3d")
