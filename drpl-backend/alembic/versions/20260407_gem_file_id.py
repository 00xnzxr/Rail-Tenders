"""Add gem_file_id column to tender_documents

Revision ID: gem_file_id_001
Revises: unified_templates_001
Create Date: 2026-04-07
"""

from alembic import op
import sqlalchemy as sa

revision = "gem_file_id_001"
down_revision = "unified_templates_001"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "tender_documents",
        sa.Column("gem_file_id", sa.String(50), nullable=True),
    )
    op.create_index("ix_tender_documents_gem_file_id", "tender_documents", ["gem_file_id"])


def downgrade():
    op.drop_index("ix_tender_documents_gem_file_id", table_name="tender_documents")
    op.drop_column("tender_documents", "gem_file_id")
