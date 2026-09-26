"""cost breakdown reconciliation gate

Revision ID: 20260712_cb_reconciliation
Revises: 20260712_boq_schedule_totals
Create Date: 2026-07-12
"""
from alembic import op
import sqlalchemy as sa

revision = "20260712_cb_reconciliation"
down_revision = "20260712_boq_schedule_totals"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "cost_breakdowns",
        sa.Column("reconciliation_json", sa.Text(), nullable=True),
    )
    op.add_column(
        "cost_breakdowns",
        sa.Column(
            "needs_review", sa.Boolean(), nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade():
    op.drop_column("cost_breakdowns", "needs_review")
    op.drop_column("cost_breakdowns", "reconciliation_json")
