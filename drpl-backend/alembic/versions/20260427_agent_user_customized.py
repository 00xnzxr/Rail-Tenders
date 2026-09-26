"""Add is_user_customized flag to custom_agents (Phase 3d-1.1)

Revision ID: agent_user_customized_001
Revises: cost_breakdown_margin_001
Create Date: 2026-04-27

When True, the agent's system_prompt + tools differ from the code canonical
(canonical_registry). The runtime resolver uses the DB version; seed scripts
skip the prompt+tools resync so user edits survive restarts.

The column defaults to False — i.e. existing agents are treated as "matches
canonical" until proven otherwise. The first-startup SHA backfill in the
seed flow flips it to True for any row whose stored prompt differs from
the code canonical (preserving any prior user edits).
"""

from alembic import op
import sqlalchemy as sa


revision = "agent_user_customized_001"
down_revision = "cost_breakdown_margin_001"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "custom_agents",
        sa.Column(
            "is_user_customized",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade():
    op.drop_column("custom_agents", "is_user_customized")
