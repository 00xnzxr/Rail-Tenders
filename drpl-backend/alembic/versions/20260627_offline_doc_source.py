"""Add source_file_path to generated_documents (offline PDF signing).

Stores the original, unsigned uploaded PDF so the offline-signing flow can
re-apply signatures from a clean source without accumulating ghost overlays.

Revision ID: offline_doc_source_001
Revises: ratecard_001
Create Date: 2026-06-27
"""
from alembic import op
import sqlalchemy as sa


revision = "offline_doc_source_001"
down_revision = "ratecard_001"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("generated_documents") as batch_op:
        batch_op.add_column(sa.Column("source_file_path", sa.Text(), nullable=True))


def downgrade():
    with op.batch_alter_table("generated_documents") as batch_op:
        batch_op.drop_column("source_file_path")
