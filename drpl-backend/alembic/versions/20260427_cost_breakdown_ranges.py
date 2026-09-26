"""Add range / per-line profit columns to cost_breakdowns + cost_breakdown_lines

Revision ID: cost_breakdown_ranges_001
Revises: generated_doc_orientation_001
Create Date: 2026-04-27

The costing agent now produces low / expected / high rate bands per line plus
per-line profit_pct (varied by category). This migration adds the new columns
as nullable so existing single-rate breakdowns keep working unchanged — the
legacy `subtotal`, `overhead_amount`, `margin_amount`, `gst_amount`,
`grand_total`, `rate`, `amount` columns continue to hold the expected band.
"""

from alembic import op
import sqlalchemy as sa


revision = "cost_breakdown_ranges_001"
down_revision = "generated_doc_orientation_001"
branch_labels = None
depends_on = None


def upgrade():
    # Range totals on cost_breakdowns (nullable — populated when range mode is in use)
    op.add_column("cost_breakdowns", sa.Column("subtotal_low", sa.Float(), nullable=True))
    op.add_column("cost_breakdowns", sa.Column("subtotal_high", sa.Float(), nullable=True))
    op.add_column("cost_breakdowns", sa.Column("overhead_amount_low", sa.Float(), nullable=True))
    op.add_column("cost_breakdowns", sa.Column("overhead_amount_high", sa.Float(), nullable=True))
    op.add_column("cost_breakdowns", sa.Column("margin_amount_low", sa.Float(), nullable=True))
    op.add_column("cost_breakdowns", sa.Column("margin_amount_high", sa.Float(), nullable=True))
    op.add_column("cost_breakdowns", sa.Column("gst_amount_low", sa.Float(), nullable=True))
    op.add_column("cost_breakdowns", sa.Column("gst_amount_high", sa.Float(), nullable=True))
    op.add_column("cost_breakdowns", sa.Column("grand_total_low", sa.Float(), nullable=True))
    op.add_column("cost_breakdowns", sa.Column("grand_total_high", sa.Float(), nullable=True))

    # Manpower & resource decomposition emitted by the costing agent
    op.add_column(
        "cost_breakdowns",
        sa.Column("manpower_resource_analysis_json", sa.Text(), nullable=True),
    )

    # Per-line range + per-line profit on cost_breakdown_lines
    op.add_column("cost_breakdown_lines", sa.Column("rate_low", sa.Float(), nullable=True))
    op.add_column("cost_breakdown_lines", sa.Column("rate_high", sa.Float(), nullable=True))
    op.add_column("cost_breakdown_lines", sa.Column("amount_low", sa.Float(), nullable=True))
    op.add_column("cost_breakdown_lines", sa.Column("amount_high", sa.Float(), nullable=True))

    op.add_column("cost_breakdown_lines", sa.Column("profit_pct", sa.Float(), nullable=True))
    op.add_column("cost_breakdown_lines", sa.Column("profit_amount_low", sa.Float(), nullable=True))
    op.add_column("cost_breakdown_lines", sa.Column("profit_amount", sa.Float(), nullable=True))
    op.add_column("cost_breakdown_lines", sa.Column("profit_amount_high", sa.Float(), nullable=True))


def downgrade():
    op.drop_column("cost_breakdown_lines", "profit_amount_high")
    op.drop_column("cost_breakdown_lines", "profit_amount")
    op.drop_column("cost_breakdown_lines", "profit_amount_low")
    op.drop_column("cost_breakdown_lines", "profit_pct")

    op.drop_column("cost_breakdown_lines", "amount_high")
    op.drop_column("cost_breakdown_lines", "amount_low")
    op.drop_column("cost_breakdown_lines", "rate_high")
    op.drop_column("cost_breakdown_lines", "rate_low")

    op.drop_column("cost_breakdowns", "manpower_resource_analysis_json")

    op.drop_column("cost_breakdowns", "grand_total_high")
    op.drop_column("cost_breakdowns", "grand_total_low")
    op.drop_column("cost_breakdowns", "gst_amount_high")
    op.drop_column("cost_breakdowns", "gst_amount_low")
    op.drop_column("cost_breakdowns", "margin_amount_high")
    op.drop_column("cost_breakdowns", "margin_amount_low")
    op.drop_column("cost_breakdowns", "overhead_amount_high")
    op.drop_column("cost_breakdowns", "overhead_amount_low")
    op.drop_column("cost_breakdowns", "subtotal_high")
    op.drop_column("cost_breakdowns", "subtotal_low")
