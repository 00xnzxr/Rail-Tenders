"""Bump claude_pdf_max_pages PlatformSetting from 100 → 200

Revision ID: pdf_max_pages_200_001
Revises: tender_analysis_structured_001
Create Date: 2026-05-15

Live deployments seeded ``claude_pdf_max_pages = 100`` before we raised the
code default to 200. The seed only runs when the row is absent, so existing
DBs are still capped at 100 — tenders with >100 cumulative pages get rejected
by the V1 native-PDF path. Bump the stored value to 200 if (and only if) it
is still at the old default. Custom-tuned values are preserved.
"""

from alembic import op
import sqlalchemy as sa


revision = "pdf_max_pages_200_001"
down_revision = "tender_analysis_structured_001"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        sa.text(
            "UPDATE platform_settings SET value = '200' "
            "WHERE key = 'claude_pdf_max_pages' AND value = '100'"
        )
    )


def downgrade():
    op.execute(
        sa.text(
            "UPDATE platform_settings SET value = '100' "
            "WHERE key = 'claude_pdf_max_pages' AND value = '200'"
        )
    )
