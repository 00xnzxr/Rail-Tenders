"""boq_items / cost_breakdown_lines — annexure component linkage

A NIT schedule item can cite an annexure as its breakdown: "Material Cost for
Conversion work (As per Annexure-II of Material list uploaded in Document
Section of NIT)". The annexure's rows are the materials that make up that ONE
item, per coach set. They are captured as BOQItems so they can be costed, and
without a link back to the item they break down they were costed as extra
scope on top of it: the estimated cost double-counted the whole annexure and
the margin view showed the components as a negative-margin "Other" bucket.

boq_items.annexure_ref        the annexure table a row was read from ("II")
boq_items.parent_item_id      the schedule item it is a component of
boq_items.source_document_id  the TenderDocument it was captured from

cost_breakdown_lines.parent_boq_item_id  same link, denormalised onto the
                                          costed line so the roll-up and the
                                          totals can see it without a join
cost_breakdown_lines.annexure_ref         group label for the editor / xlsx

All nullable: a tender with no annexures never sets any of them.

Revision ID: 20260911_boq_annexure_components
Revises: 20260909_run_partial_output
"""

from alembic import op
import sqlalchemy as sa


revision = "20260911_boq_annexure_components"
down_revision = "20260909_run_partial_output"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("boq_items", sa.Column("annexure_ref", sa.String(32), nullable=True))
    op.add_column("boq_items", sa.Column("parent_item_id", sa.Integer(), nullable=True))
    op.add_column("boq_items", sa.Column("source_document_id", sa.Integer(), nullable=True))
    op.create_index("ix_boq_items_annexure_ref", "boq_items", ["annexure_ref"])
    op.create_index("ix_boq_items_parent_item_id", "boq_items", ["parent_item_id"])

    op.add_column(
        "cost_breakdown_lines",
        sa.Column(
            "parent_boq_item_id",
            sa.Integer(),
            sa.ForeignKey("boq_items.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column("cost_breakdown_lines", sa.Column("annexure_ref", sa.String(32), nullable=True))
    op.create_index(
        "ix_cost_breakdown_lines_parent_boq_item_id",
        "cost_breakdown_lines",
        ["parent_boq_item_id"],
    )


def downgrade():
    op.drop_index("ix_cost_breakdown_lines_parent_boq_item_id", table_name="cost_breakdown_lines")
    op.drop_column("cost_breakdown_lines", "annexure_ref")
    op.drop_column("cost_breakdown_lines", "parent_boq_item_id")
    op.drop_index("ix_boq_items_parent_item_id", table_name="boq_items")
    op.drop_index("ix_boq_items_annexure_ref", table_name="boq_items")
    op.drop_column("boq_items", "source_document_id")
    op.drop_column("boq_items", "parent_item_id")
    op.drop_column("boq_items", "annexure_ref")
