"""Drop tender_analysis_summaries.structured_analysis JSON column

Revision ID: drop_structured_analysis_001
Revises: nit_aware_costing_001
Create Date: 2026-05-29

The tender analyzer now produces only a markdown report. The duplicated
structured JSON (requirements / negative_keywords / required_documents /
summary / key_risks / cross_document_conflicts / missing_items /
action_items) is no longer generated, persisted, or rendered — the
artifact panel renders ``requirement_summary`` directly.
"""

from alembic import op
import sqlalchemy as sa


revision = "drop_structured_analysis_001"
down_revision = "nit_aware_costing_001"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("tender_analysis_summaries") as batch_op:
        batch_op.drop_column("structured_analysis")


def downgrade():
    op.add_column(
        "tender_analysis_summaries",
        sa.Column("structured_analysis", sa.JSON(), nullable=True),
    )
