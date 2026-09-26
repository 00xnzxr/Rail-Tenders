"""cost_line_oem_source_url

Revision ID: 7f6ae5991d3c
Revises: f92bd2162176
Create Date: 2026-07-20 18:10:30.305569
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


revision: str = '7f6ae5991d3c'
down_revision: Union[str, None] = 'f92bd2162176'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("cost_breakdown_lines",
                  sa.Column("oem_manufacturer", sa.String(length=128), nullable=True))
    op.add_column("cost_breakdown_lines",
                  sa.Column("source_url", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("cost_breakdown_lines", "source_url")
    op.drop_column("cost_breakdown_lines", "oem_manufacturer")
