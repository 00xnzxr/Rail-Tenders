"""Add tender_analysis_summaries.structured_analysis JSON column

Revision ID: tender_analysis_structured_001
Revises: tender_scope_profile_001
Create Date: 2026-05-15

Stores the V1-shape synthesis JSON tail (requirements / negative_keywords /
required_documents / summary / key_risks / cross_document_conflicts /
missing_items / action_items) so the chat-path bridge can surface it to the
artifact panel as ``structured_data``. Without this column, V2 analyzer runs
return only the markdown ``report_markdown`` and the frontend renderer falls
back to raw markdown bullets instead of the styled collapsible view.
"""

from alembic import op
import sqlalchemy as sa


revision = "tender_analysis_structured_001"
down_revision = "tender_scope_profile_001"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "tender_analysis_summaries",
        sa.Column("structured_analysis", sa.JSON(), nullable=True),
    )


def downgrade():
    op.drop_column("tender_analysis_summaries", "structured_analysis")
