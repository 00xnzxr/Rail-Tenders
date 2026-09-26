"""NIT-aware costing — extend BOQItem to be a faithful NIT-schedule row,
extend CostBreakdownLine to mirror it 1:1, and add a FK so cost lines can
be rebound to their source schedule row after a BOQ re-parse.

Revision ID: nit_aware_costing_001
Revises: notifications_001
Create Date: 2026-05-28

The DRPL platform's costing agent previously emitted its own internal
schema (CostBreakdownLine) with no relationship to the tender's bidding
schedule. The user can't directly hand that to procurement — the IREPS /
GeM submission needs the exact NIT shape: S.No. / Item Code / Description /
Item Qty / Qty Unit / Unit Rate / Basic Value / Escl.(%) / Amount / Bidding
Unit, grouped by Schedule (A, B, ...), with GST as a distinct schedule line.

This migration adds the persistence for capturing the NIT schedule
(BOQItem) and for the cost agent to emit a 1:1 mirror (CostBreakdownLine
with boq_item_id FK + denormalised NIT columns). All new columns nullable
so legacy rows keep working.

Plan: ~/.claude/plans/now-i-need-to-synchronous-taco.md
"""

from alembic import op
import sqlalchemy as sa


revision = "nit_aware_costing_001"
down_revision = "notifications_001"
branch_labels = None
depends_on = None


def upgrade():
    # BOQItem — full NIT schedule row.
    op.add_column("boq_items", sa.Column("item_code", sa.String(length=64), nullable=True))
    op.add_column("boq_items", sa.Column("schedule_name", sa.String(length=64), nullable=True))
    op.create_index("ix_boq_items_schedule_name", "boq_items", ["schedule_name"])
    op.add_column("boq_items", sa.Column("bidding_unit", sa.String(length=64), nullable=True))
    op.add_column("boq_items", sa.Column("basic_value", sa.Float(), nullable=True))
    op.add_column("boq_items", sa.Column("escalation_pct", sa.Float(), nullable=True))
    op.add_column(
        "boq_items",
        sa.Column("is_tax_line", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("boq_items", sa.Column("extraction_confidence", sa.String(length=16), nullable=True))

    # CostBreakdownLine — denormalised mirror of the BOQItem above plus the FK.
    op.add_column("cost_breakdown_lines", sa.Column("boq_item_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_cost_breakdown_lines_boq_item_id",
        "cost_breakdown_lines",
        "boq_items",
        ["boq_item_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_cost_breakdown_lines_boq_item_id",
        "cost_breakdown_lines",
        ["boq_item_id"],
    )
    op.add_column("cost_breakdown_lines", sa.Column("item_code", sa.String(length=64), nullable=True))
    op.add_column("cost_breakdown_lines", sa.Column("schedule_name", sa.String(length=64), nullable=True))
    op.create_index(
        "ix_cost_breakdown_lines_schedule_name",
        "cost_breakdown_lines",
        ["schedule_name"],
    )
    op.add_column("cost_breakdown_lines", sa.Column("bidding_unit", sa.String(length=64), nullable=True))
    op.add_column("cost_breakdown_lines", sa.Column("basic_value", sa.Float(), nullable=True))
    op.add_column("cost_breakdown_lines", sa.Column("escalation_pct", sa.Float(), nullable=True))
    op.add_column(
        "cost_breakdown_lines",
        sa.Column("is_tax_line", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade():
    op.drop_column("cost_breakdown_lines", "is_tax_line")
    op.drop_column("cost_breakdown_lines", "escalation_pct")
    op.drop_column("cost_breakdown_lines", "basic_value")
    op.drop_column("cost_breakdown_lines", "bidding_unit")
    op.drop_index("ix_cost_breakdown_lines_schedule_name", table_name="cost_breakdown_lines")
    op.drop_column("cost_breakdown_lines", "schedule_name")
    op.drop_column("cost_breakdown_lines", "item_code")
    op.drop_index("ix_cost_breakdown_lines_boq_item_id", table_name="cost_breakdown_lines")
    op.drop_constraint(
        "fk_cost_breakdown_lines_boq_item_id", "cost_breakdown_lines", type_="foreignkey"
    )
    op.drop_column("cost_breakdown_lines", "boq_item_id")

    op.drop_column("boq_items", "extraction_confidence")
    op.drop_column("boq_items", "is_tax_line")
    op.drop_column("boq_items", "escalation_pct")
    op.drop_column("boq_items", "basic_value")
    op.drop_column("boq_items", "bidding_unit")
    op.drop_index("ix_boq_items_schedule_name", table_name="boq_items")
    op.drop_column("boq_items", "schedule_name")
    op.drop_column("boq_items", "item_code")
