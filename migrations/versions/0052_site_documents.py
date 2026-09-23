"""site_documents: юридические документы сайта (/legal/privacy, /legal/terms), редактируются в web-admin."""

from __future__ import annotations

from alembic import op

revision = "0052_site_documents"
down_revision = "0051_user_offers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS site_documents (
            slug VARCHAR(32) PRIMARY KEY,
            title VARCHAR(255) NOT NULL,
            content_html TEXT NOT NULL,
            source_url VARCHAR(500) NULL,
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS site_documents")
