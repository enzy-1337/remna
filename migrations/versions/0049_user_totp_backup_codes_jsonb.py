"""users.site_totp_backup_codes: json → jsonb.

У типа json в PostgreSQL нет оператора равенства — любой SELECT DISTINCT по строке users падал с
«could not identify an equality operator for type json» (спам ошибок connection_notify_loop).
"""

from __future__ import annotations

from alembic import op

revision = "0049_user_totp_codes_jsonb"
down_revision = "0048_user_email_mkt_consent"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE users ALTER COLUMN site_totp_backup_codes TYPE jsonb USING site_totp_backup_codes::jsonb")


def downgrade() -> None:
    op.execute("ALTER TABLE users ALTER COLUMN site_totp_backup_codes TYPE json USING site_totp_backup_codes::json")
