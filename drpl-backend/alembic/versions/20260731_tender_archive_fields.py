"""tender archive fields

Revision ID: 20260731_tender_archive
Revises: 7f6ae5991d3c
Create Date: 2026-07-31
"""
from alembic import op
import sqlalchemy as sa

revision = "20260731_tender_archive"
down_revision = "7f6ae5991d3c"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("tenders", sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("tenders", sa.Column("archive_reason", sa.String(30), nullable=True))
    op.create_index("ix_tenders_archived_at", "tenders", ["archived_at"])


def downgrade():
    op.drop_index("ix_tenders_archived_at", table_name="tenders")
    op.drop_column("tenders", "archive_reason")
    op.drop_column("tenders", "archived_at")
