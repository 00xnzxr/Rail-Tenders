"""boq_schedule_reconciliation_status

Revision ID: f92bd2162176
Revises: 20260712_eager_analysis
Create Date: 2026-07-20 11:37:41.238050
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


revision: str = 'f92bd2162176'
down_revision: Union[str, None] = '20260712_eager_analysis'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("boq_schedule_totals",
                  sa.Column("reconciliation_status", sa.String(length=16), nullable=True))


def downgrade() -> None:
    op.drop_column("boq_schedule_totals", "reconciliation_status")
