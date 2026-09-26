"""agent_runs.partial_output / partial_trace — what a run had produced when it stopped

Streamed text lived only in the Redis event stream, which expires an hour after
the run ends, and the final answer was saved only when a run *finished*. A run
killed at token 2,900 of 3,000 — a deploy, a crash, a cancel — therefore left
nothing readable once Redis forgot it: a row saying `cancelled` and no text.

The worker now writes the text streamed so far (`partial_output`) and the
compact step list from the Master's timeline (`partial_trace`) to the row as it
goes, and a run that ends without finishing has that written into the session
history where the user already looks.

Both nullable: a run that finishes normally has its answer in the session
transcript and needs neither.

Revision ID: 20260909_run_partial_output
Revises: 20260903_usage_run_id
"""

from alembic import op
import sqlalchemy as sa


revision = "20260909_run_partial_output"
down_revision = "20260903_usage_run_id"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "agent_runs",
        sa.Column("partial_output", sa.Text(), nullable=True),
    )
    op.add_column(
        "agent_runs",
        sa.Column("partial_trace", sa.JSON(), nullable=True),
    )


def downgrade():
    op.drop_column("agent_runs", "partial_trace")
    op.drop_column("agent_runs", "partial_output")
