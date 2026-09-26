"""tender segment + segment_overridden

Revision ID: 20260709_tender_segment
Revises: 20260708_auto_scoring
Create Date: 2026-07-09
"""
from alembic import op
import sqlalchemy as sa

revision = "20260709_tender_segment"
down_revision = "20260708_auto_scoring"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("tenders", sa.Column("segment", sa.String(length=20), nullable=True))
    op.add_column("tenders", sa.Column("segment_overridden", sa.Boolean(), server_default=sa.false(), nullable=False))


def downgrade():
    op.drop_column("tenders", "segment_overridden")
    op.drop_column("tenders", "segment")
