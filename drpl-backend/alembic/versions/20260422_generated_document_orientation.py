"""Add page_orientation column to generated_documents

Revision ID: generated_doc_orientation_001
Revises: page_orientation_001
Create Date: 2026-04-22
"""

from alembic import op
import sqlalchemy as sa

revision = "generated_doc_orientation_001"
down_revision = "page_orientation_001"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "generated_documents",
        sa.Column(
            "page_orientation",
            sa.String(length=16),
            nullable=False,
            server_default="portrait",
        ),
    )


def downgrade():
    op.drop_column("generated_documents", "page_orientation")
