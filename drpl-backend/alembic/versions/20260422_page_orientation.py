"""Add page_orientation column to document_workspaces

Revision ID: page_orientation_001
Revises: composite_indexes_001
Create Date: 2026-04-22
"""

from alembic import op
import sqlalchemy as sa

revision = "page_orientation_001"
down_revision = "composite_indexes_001"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "document_workspaces",
        sa.Column(
            "page_orientation",
            sa.String(length=16),
            nullable=False,
            server_default="portrait",
        ),
    )


def downgrade():
    op.drop_column("document_workspaces", "page_orientation")
