"""eager analysis status

Revision ID: 20260712_eager_analysis
Revises: 20260712_cb_reconciliation
Create Date: 2026-07-12
"""
from alembic import op
import sqlalchemy as sa

revision = "20260712_eager_analysis"
down_revision = "20260712_cb_reconciliation"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("tenders", sa.Column("eager_analysis_status", sa.String(16), nullable=True))
    op.add_column("tenders", sa.Column("eager_analysis_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_tenders_eager_analysis_status", "tenders", ["eager_analysis_status"])


def downgrade():
    op.drop_index("ix_tenders_eager_analysis_status", table_name="tenders")
    op.drop_column("tenders", "eager_analysis_at")
    op.drop_column("tenders", "eager_analysis_status")
