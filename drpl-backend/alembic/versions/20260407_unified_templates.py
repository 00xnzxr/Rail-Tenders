"""Add output_format and file columns to template tables for unified template management

Revision ID: unified_templates_001
Revises:
Create Date: 2026-04-07
"""

from alembic import op
import sqlalchemy as sa

revision = "unified_templates_001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    # ProposalTemplate: add output_format
    op.add_column(
        "proposal_templates",
        sa.Column("output_format", sa.String(10), server_default="docx", nullable=True),
    )

    # DocumentFormatTemplate: add output_format + file tracking
    op.add_column(
        "document_format_templates",
        sa.Column("output_format", sa.String(10), server_default="docx", nullable=True),
    )
    op.add_column(
        "document_format_templates",
        sa.Column("original_file_path", sa.Text(), nullable=True),
    )
    op.add_column(
        "document_format_templates",
        sa.Column("original_file_name", sa.String(500), nullable=True),
    )

    # CostingTemplate: add output_format + file name
    op.add_column(
        "costing_templates",
        sa.Column("output_format", sa.String(10), server_default="xlsx", nullable=True),
    )
    op.add_column(
        "costing_templates",
        sa.Column("original_file_name", sa.String(500), nullable=True),
    )

    # Set output_format='xlsx' for BOQ document format templates
    op.execute(
        "UPDATE document_format_templates SET output_format = 'xlsx' "
        "WHERE document_category = 'boq'"
    )


def downgrade():
    op.drop_column("costing_templates", "original_file_name")
    op.drop_column("costing_templates", "output_format")
    op.drop_column("document_format_templates", "original_file_name")
    op.drop_column("document_format_templates", "original_file_path")
    op.drop_column("document_format_templates", "output_format")
    op.drop_column("proposal_templates", "output_format")
