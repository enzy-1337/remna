"""users: email_marketing_consent — согласие на рекламную/информационную рассылку по почте."""

from __future__ import annotations

from alembic import op

revision = "0048_user_email_mkt_consent"
down_revision = "0047_sub_expiry_email_flags"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS email_marketing_consent boolean NOT NULL DEFAULT false")
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS email_marketing_consent_at TIMESTAMP WITH TIME ZONE NULL")


def downgrade() -> None:
    op.drop_column("users", "email_marketing_consent_at")
    op.drop_column("users", "email_marketing_consent")
