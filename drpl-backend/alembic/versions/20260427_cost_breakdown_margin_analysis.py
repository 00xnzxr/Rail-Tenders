"""Add tender-rate / margin / schedule_section / strategic_summary columns
for Phase 3b reference-Excel structural alignment

Revision ID: cost_breakdown_margin_001
Revises: cost_breakdown_ranges_001
Create Date: 2026-04-27

The user's NFR Lumding reference workbook compares the tender's stated BOQ
rate against DRPL's estimated cost to surface gross margin per line, grouped
by schedule (A/B/C), with a Sheet 1 strategic summary and Sheet 2 cost
assumptions library. This migration adds the persistence layer for that:

cost_breakdowns:
  - tender_total            : sum of tender_amount across priced lines
  - margin_total_low/high   : tender_total - subtotal per band
  - margin_total            : expected band (tender_total - subtotal_expected)
  - margin_pct              : margin_total / tender_total × 100
  - cost_assumptions_json   : flat rate-card library (reference Sheet 2)
  - strategic_summary_json  : tender snapshot + observations + recommended bid

cost_breakdown_lines:
  - tender_rate             : the rate stated in the tender BOQ for this line
  - tender_amount           : tender_rate × quantity
  - margin_amount_low/high  : tender_amount - amount per band
  - margin_amount           : expected band (tender_amount - amount_expected)
  - margin_pct              : margin_amount / tender_amount × 100
  - schedule_section        : grouping label ("Schedule A — Part 1", etc.)
  - cost_buildup_note       : short explanation of unit-cost build-up

All new columns nullable so existing breakdowns (range-only or single-rate)
keep working unchanged.
"""

from alembic import op
import sqlalchemy as sa


revision = "cost_breakdown_margin_001"
down_revision = "cost_breakdown_ranges_001"
branch_labels = None
depends_on = None


def upgrade():
    # cost_breakdowns — top-level margin totals + new JSON columns
    op.add_column("cost_breakdowns", sa.Column("tender_total", sa.Float(), nullable=True))
    op.add_column("cost_breakdowns", sa.Column("margin_total_low", sa.Float(), nullable=True))
    op.add_column("cost_breakdowns", sa.Column("margin_total", sa.Float(), nullable=True))
    op.add_column("cost_breakdowns", sa.Column("margin_total_high", sa.Float(), nullable=True))
    op.add_column("cost_breakdowns", sa.Column("margin_pct", sa.Float(), nullable=True))
    op.add_column("cost_breakdowns", sa.Column("cost_assumptions_json", sa.Text(), nullable=True))
    op.add_column("cost_breakdowns", sa.Column("strategic_summary_json", sa.Text(), nullable=True))

    # cost_breakdown_lines — per-line tender / margin / schedule fields
    op.add_column("cost_breakdown_lines", sa.Column("tender_rate", sa.Float(), nullable=True))
    op.add_column("cost_breakdown_lines", sa.Column("tender_amount", sa.Float(), nullable=True))
    op.add_column("cost_breakdown_lines", sa.Column("margin_amount_low", sa.Float(), nullable=True))
    op.add_column("cost_breakdown_lines", sa.Column("margin_amount", sa.Float(), nullable=True))
    op.add_column("cost_breakdown_lines", sa.Column("margin_amount_high", sa.Float(), nullable=True))
    op.add_column("cost_breakdown_lines", sa.Column("margin_pct", sa.Float(), nullable=True))
    op.add_column(
        "cost_breakdown_lines",
        sa.Column("schedule_section", sa.String(length=128), nullable=True),
    )
    op.create_index(
        "ix_cost_breakdown_lines_schedule_section",
        "cost_breakdown_lines",
        ["schedule_section"],
    )
    op.add_column("cost_breakdown_lines", sa.Column("cost_buildup_note", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("cost_breakdown_lines", "cost_buildup_note")
    op.drop_index("ix_cost_breakdown_lines_schedule_section", table_name="cost_breakdown_lines")
    op.drop_column("cost_breakdown_lines", "schedule_section")
    op.drop_column("cost_breakdown_lines", "margin_pct")
    op.drop_column("cost_breakdown_lines", "margin_amount_high")
    op.drop_column("cost_breakdown_lines", "margin_amount")
    op.drop_column("cost_breakdown_lines", "margin_amount_low")
    op.drop_column("cost_breakdown_lines", "tender_amount")
    op.drop_column("cost_breakdown_lines", "tender_rate")

    op.drop_column("cost_breakdowns", "strategic_summary_json")
    op.drop_column("cost_breakdowns", "cost_assumptions_json")
    op.drop_column("cost_breakdowns", "margin_pct")
    op.drop_column("cost_breakdowns", "margin_total_high")
    op.drop_column("cost_breakdowns", "margin_total")
    op.drop_column("cost_breakdowns", "margin_total_low")
    op.drop_column("cost_breakdowns", "tender_total")
