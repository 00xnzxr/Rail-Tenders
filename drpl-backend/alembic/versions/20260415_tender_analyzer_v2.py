"""Add tender analyzer v2 columns: per-doc summary_json + summary cost/version tracking

Revision ID: tender_analyzer_v2
Revises: workflows_001
Create Date: 2026-04-15
"""

from alembic import op
import sqlalchemy as sa

revision = "tender_analyzer_v2"
down_revision = "workflows_001"
branch_labels = None
depends_on = None


def upgrade():
    # Per-document compact summary (the Haiku output blob — see
    # _analyze_single_document_native in document_analysis_agent.py).
    # Synthesis reads from this column instead of re-reading raw PDFs.
    op.add_column(
        "document_extraction_results",
        sa.Column("summary_json", sa.JSON(), nullable=True),
    )

    # v2 cost / version tracking on the per-tender summary row.
    op.add_column(
        "tender_analysis_summaries",
        sa.Column("total_input_tokens", sa.Integer(), nullable=True),
    )
    op.add_column(
        "tender_analysis_summaries",
        sa.Column("total_output_tokens", sa.Integer(), nullable=True),
    )
    op.add_column(
        "tender_analysis_summaries",
        sa.Column("total_cost_usd", sa.Numeric(10, 4), nullable=True),
    )
    op.add_column(
        "tender_analysis_summaries",
        sa.Column("analysis_version", sa.String(8), nullable=True),
    )
    op.add_column(
        "tender_analysis_summaries",
        sa.Column("per_doc_unreadable_count", sa.Integer(), nullable=True),
    )


def downgrade():
    op.drop_column("tender_analysis_summaries", "per_doc_unreadable_count")
    op.drop_column("tender_analysis_summaries", "analysis_version")
    op.drop_column("tender_analysis_summaries", "total_cost_usd")
    op.drop_column("tender_analysis_summaries", "total_output_tokens")
    op.drop_column("tender_analysis_summaries", "total_input_tokens")
    op.drop_column("document_extraction_results", "summary_json")
