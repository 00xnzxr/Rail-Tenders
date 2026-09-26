"""Composite indexes on hot query paths.

Revision ID: composite_indexes_001
Revises: agent_runs_001
Create Date: 2026-04-19

Phase C3: indexes sized to actual list endpoints. Each one matches a
multi-column WHERE/ORDER BY that shows up in a hot request path:

- tenders(status, closing_date)     — "open tenders sorted by due date"
- tenders(assigned_to, workflow_status) — "my in-progress tenders"
- agent_tools(tool_type, is_active) — /api/agent-builder/tools filter
- custom_agents(category, is_published) — /api/agent-builder/agents filter
- proposal_sessions(created_by, updated_at) — list_sessions ORDER BY
- proposal_messages(session_id, created_at) — conversation load

Uses IF NOT EXISTS so it re-runs safely on environments that hand-rolled
a subset of these previously.
"""

from alembic import op


revision = "composite_indexes_001"
down_revision = "agent_runs_001"
branch_labels = None
depends_on = None


_INDEXES = [
    ("ix_tenders_status_closing", "tenders", "(status, closing_date)"),
    ("ix_tenders_assignee_workflow", "tenders", "(assigned_to, workflow_status)"),
    ("ix_agent_tools_type_active", "agent_tools", "(tool_type, is_active)"),
    ("ix_custom_agents_category_published", "custom_agents", "(category, is_published)"),
    ("ix_proposal_sessions_user_updated", "proposal_sessions", "(created_by, updated_at)"),
    ("ix_proposal_messages_session_created", "proposal_messages", "(session_id, created_at)"),
]


def upgrade() -> None:
    for name, table, cols in _INDEXES:
        op.execute(f'CREATE INDEX IF NOT EXISTS "{name}" ON "{table}" {cols}')


def downgrade() -> None:
    for name, _table, _cols in _INDEXES:
        op.execute(f'DROP INDEX IF EXISTS "{name}"')
