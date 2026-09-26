"""boq_schedule_totals.title — the schedule banner as printed

An IREPS schedule banner says what its rows price and on what basis:
"B-COST OF LABOUR: Mid Life Rehabilitation of LHB Coaches ... (INCLUSIVE OF ALL
TAXES AND CHARGES)". Schedule A of the same NIT lists "Web to Drg. No.
LE11185" as the web's material cost (Rs 279.66) and schedule B lists the same
words as the labour to fit it (Rs 2,239.21). The capture kept only the letter,
so the costing could not tell a material row from a labour row, or a rate
with GST in it from one without.

Nullable: a tender captured before this, or by the AI path, has no title.

Revision ID: 20260925_boq_schedule_title
Revises: 20260911_boq_annexure_components
"""

from alembic import op
import sqlalchemy as sa


revision = "20260925_boq_schedule_title"
down_revision = "20260911_boq_annexure_components"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("boq_schedule_totals", sa.Column("title", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("boq_schedule_totals", "title")
