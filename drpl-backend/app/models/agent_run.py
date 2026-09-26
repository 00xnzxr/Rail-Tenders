"""
DRPL Backend - AgentRun model.

Phase C infra: explicit run-id tracking so an agent execution can survive a
browser refresh, be resumed, and be queried for status after the SSE
connection is gone. Each enqueue allocates one row; the RQ worker updates
status/finished_at as the run progresses. The UUID lives in both the URL
(`GET /runs/{run_id}/events`) and the Redis pub/sub channel
(`user:{user_id}:run:{run_id}`).

Runs are user-scoped by design so a future per-tenant deployment can index
and authorise by `user_id` without schema churn.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from sqlalchemy import (
    Column, String, Integer, Text, DateTime, Index, JSON,
)

from app.core.database import Base


class AgentRun(Base):
    """One row per enqueued agent run (router or specialised)."""
    __tablename__ = "agent_runs"

    # UUID primary key — issued at enqueue time and returned to the client
    # before the worker has even picked the job up, so the UI can open the
    # SSE stream immediately.
    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))

    # Ownership + scoping. user_id is intentionally not an FK (keeps this
    # table portable if we ever split auth into its own service) but we
    # always filter by it in queries.
    user_id = Column(Integer, nullable=False, index=True)

    # Context the run was started with. proposal_session_id is the
    # Command Center session UI-side; router_session_id is the in-process
    # conversation key used by streaming_handler.
    proposal_session_id = Column(Integer, nullable=True, index=True)
    router_session_id = Column(String(64), nullable=True, index=True)
    tender_id = Column(Integer, nullable=True, index=True)

    # "queued" → "running" → "completed" | "failed" | "cancelled"
    status = Column(String(20), nullable=False, default="queued", index=True)

    # The prompt that kicked this run off (truncated to a reasonable cap
    # upstream). Handy for the runs list UI and debugging.
    prompt = Column(Text, nullable=True)
    display_message = Column(Text, nullable=True)

    # Which agent(s) the router dispatched to, as a JSON list. None until
    # routing has happened.
    selected_agents = Column(JSON, nullable=True)

    # Terminal fields populated when the worker finishes.
    error_message = Column(Text, nullable=True)
    result_summary = Column(Text, nullable=True)

    # What the run had produced so far, written by the worker as it streams.
    # The streamed text otherwise lives only in the Redis event stream, which
    # expires an hour after the run ends, and the final answer is saved only
    # when a run *finishes* — so a run killed at token 2,900 of 3,000 left
    # nothing readable once Redis forgot it. `partial_output` is the
    # assistant text streamed so far (token events, honouring token_reset);
    # `partial_trace` is the compact step list ({type, step, tool, is_error})
    # the Master's timeline emits, so a stopped run can still say which steps
    # had completed. Both nullable; both stay on the row after it ends.
    partial_output = Column(Text, nullable=True)
    partial_trace = Column(JSON, nullable=True)

    # Link back to RQ so we can look up / cancel / inspect a running job.
    rq_job_id = Column(String(64), nullable=True, index=True)

    # Free-form metadata (file_metadata payload, feature flags, etc).
    meta = Column(JSON, nullable=True)

    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
        index=True,
    )
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_agent_runs_user_created", "user_id", "created_at"),
        Index("ix_agent_runs_user_status", "user_id", "status"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"<AgentRun id={self.id} user={self.user_id} "
            f"status={self.status} session={self.proposal_session_id}>"
        )
