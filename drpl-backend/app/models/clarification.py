"""
DRPL Backend - Pending Clarifications
Agent-initiated questions that pause a run until the user answers.
Phase B (clarification popups) — the checkpointer/resume step is deferred
to Phase C; for now answers are injected as extra context on the next
router turn.
"""

from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Text, DateTime, JSON
from app.core.database import Base


class PendingClarification(Base):
    __tablename__ = "pending_clarifications"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, nullable=False, index=True)              # FK to proposal_sessions.id
    router_session_id = Column(String(100), nullable=True, index=True)    # AgentConversationHistory.session_id linkage
    agent_key = Column(String(100), nullable=False)                       # e.g. costing_researcher, tender_analyzer
    question = Column(Text, nullable=False)
    options = Column(JSON, nullable=True)                                 # Optional list[str] of suggested answers
    context = Column(JSON, nullable=True)                                 # Freeform context (original task, tool args, etc.)
    status = Column(String(20), nullable=False, default="pending", index=True)  # pending | answered | cancelled
    answer = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    answered_at = Column(DateTime(timezone=True), nullable=True)
