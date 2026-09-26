"""Add detailed-card fields to tenders (location, bid_type, source_portal, category).

Revision ID: tender_card_fields_001
Revises: offline_doc_source_001
Create Date: 2026-07-07
"""
from alembic import op
import sqlalchemy as sa


revision = "tender_card_fields_001"
down_revision = "offline_doc_source_001"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("tenders") as batch_op:
        batch_op.add_column(sa.Column("location", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("bid_type", sa.String(length=50), nullable=True))
        batch_op.add_column(sa.Column("source_portal", sa.String(length=50), nullable=True))
        batch_op.add_column(sa.Column("category", sa.String(length=255), nullable=True))


def downgrade():
    with op.batch_alter_table("tenders") as batch_op:
        batch_op.drop_column("category")
        batch_op.drop_column("source_portal")
        batch_op.drop_column("bid_type")
        batch_op.drop_column("location")
