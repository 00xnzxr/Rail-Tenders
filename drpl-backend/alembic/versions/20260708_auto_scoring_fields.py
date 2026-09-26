"""auto-scoring fields: below_threshold, scoring_attempts

Revision ID: 20260708_auto_scoring
Revises: tender_card_fields_001
Create Date: 2026-07-08
"""
from alembic import op
import sqlalchemy as sa

revision = "20260708_auto_scoring"
down_revision = "tender_card_fields_001"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("tenders", sa.Column("below_threshold", sa.Boolean(), server_default=sa.false(), nullable=False))
    op.add_column("tenders", sa.Column("scoring_attempts", sa.Integer(), server_default="0", nullable=False))


def downgrade():
    op.drop_column("tenders", "scoring_attempts")
    op.drop_column("tenders", "below_threshold")
