"""Ratecard tables (structured Part-No price lists + check-schedule kits) and
component build-up costing columns.

Revision ID: ratecard_001
Revises: drop_structured_analysis_001
Create Date: 2026-06-26

The costing agent previously produced a 1:1 mirror of the NIT bidding schedule
and had no structured rate source. The client's real costing is a bottom-up
component build-up: an NIT scope item ("D-check on VTA 28L") expands into 80+
constituent spare parts, each priced from a Cummins/Fleetguard ratecard
(Part No -> Rate) and grouped into Annexures A-L.

This migration adds:
  - ratecards / ratecard_check_schedules / ratecard_items — the structured
    ratecard store, ingested from Excel.
  - cost_breakdown_lines.annexure — component grouping for the client layout.
  - cost_breakdowns.cost_sheet_template — selectable output layout.

All new columns nullable so legacy rows keep working.
"""

from alembic import op
import sqlalchemy as sa


revision = "ratecard_001"
down_revision = "drop_structured_analysis_001"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "ratecards",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("source_label", sa.String(length=64), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="active"),
        sa.Column("original_file_name", sa.String(length=500), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("name", name="uq_ratecards_name"),
    )
    op.create_index("ix_ratecards_id", "ratecards", ["id"])

    op.create_table(
        "ratecard_check_schedules",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ratecard_id", sa.Integer(), nullable=False),
        sa.Column("engine_type", sa.String(length=64), nullable=True),
        sa.Column("check_level", sa.String(length=16), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column("nit_scope_keywords", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["ratecard_id"], ["ratecards.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_ratecard_check_schedules_id", "ratecard_check_schedules", ["id"])
    op.create_index("ix_ratecard_check_schedules_ratecard_id", "ratecard_check_schedules", ["ratecard_id"])
    op.create_index("ix_ratecard_check_schedules_engine_type", "ratecard_check_schedules", ["engine_type"])
    op.create_index("ix_rc_check_engine_level", "ratecard_check_schedules", ["engine_type", "check_level"])

    op.create_table(
        "ratecard_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ratecard_id", sa.Integer(), nullable=False),
        sa.Column("check_schedule_id", sa.Integer(), nullable=True),
        sa.Column("engine_type", sa.String(length=64), nullable=True),
        sa.Column("part_no", sa.String(length=128), nullable=True),
        sa.Column("part_no_norm", sa.String(length=128), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("uom", sa.String(length=32), nullable=True),
        sa.Column("qty", sa.Float(), nullable=True),
        sa.Column("rate", sa.Float(), nullable=True),
        sa.Column("source", sa.String(length=64), nullable=True),
        sa.Column("remark", sa.Text(), nullable=True),
        sa.Column("is_mandatory", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("annexure", sa.String(length=8), nullable=True),
        sa.Column("sr_no", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["ratecard_id"], ["ratecards.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["check_schedule_id"], ["ratecard_check_schedules.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_ratecard_items_id", "ratecard_items", ["id"])
    op.create_index("ix_ratecard_items_ratecard_id", "ratecard_items", ["ratecard_id"])
    op.create_index("ix_ratecard_items_check_schedule_id", "ratecard_items", ["check_schedule_id"])
    op.create_index("ix_ratecard_items_engine_type", "ratecard_items", ["engine_type"])
    op.create_index("ix_ratecard_items_part_no", "ratecard_items", ["part_no"])
    op.create_index("ix_ratecard_items_part_norm", "ratecard_items", ["part_no_norm"])
    op.create_index("ix_ratecard_items_engine_check", "ratecard_items", ["engine_type", "check_schedule_id"])

    # Component build-up costing columns.
    op.add_column("cost_breakdown_lines", sa.Column("annexure", sa.String(length=8), nullable=True))
    op.create_index("ix_cost_breakdown_lines_annexure", "cost_breakdown_lines", ["annexure"])
    op.add_column("cost_breakdowns", sa.Column("cost_sheet_template", sa.String(length=32), nullable=True))


def downgrade():
    op.drop_column("cost_breakdowns", "cost_sheet_template")
    op.drop_index("ix_cost_breakdown_lines_annexure", table_name="cost_breakdown_lines")
    op.drop_column("cost_breakdown_lines", "annexure")

    op.drop_table("ratecard_items")
    op.drop_table("ratecard_check_schedules")
    op.drop_table("ratecards")
