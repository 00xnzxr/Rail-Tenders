"""boq schedule totals

Revision ID: 20260712_boq_schedule_totals
Revises: 20260709_tender_segment
Create Date: 2026-07-12
"""
from alembic import op
import sqlalchemy as sa

revision = "20260712_boq_schedule_totals"
down_revision = "20260709_tender_segment"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "boq_schedule_totals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tender_id", sa.Integer(), nullable=False),
        sa.Column("schedule_code", sa.String(64), nullable=False),
        sa.Column("stated_total", sa.Float(), nullable=True),
        sa.Column("advertised_value", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_boq_schedule_totals_tender_id", "boq_schedule_totals", ["tender_id"]
    )
    op.create_index(
        "ix_boq_schedule_totals_id", "boq_schedule_totals", ["id"]
    )


def downgrade():
    op.drop_index("ix_boq_schedule_totals_id", table_name="boq_schedule_totals")
    op.drop_index("ix_boq_schedule_totals_tender_id", table_name="boq_schedule_totals")
    op.drop_table("boq_schedule_totals")
