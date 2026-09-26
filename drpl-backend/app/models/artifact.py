"""
DRPL Backend - Command Center Artifact Model
Versioned structured outputs (documents, cost breakdowns, checklists, analyses)
produced by agents during Command Center chat sessions.
"""

from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Text, DateTime, JSON, Boolean, Index

from app.core.database import Base


class CommandCenterArtifact(Base):
    __tablename__ = "command_center_artifacts"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, nullable=False, index=True)            # FK to proposal_sessions.id
    artifact_type = Column(String(50), nullable=False)                  # document, cost_breakdown, checklist, analysis
    title = Column(String(500), nullable=False)
    content = Column(Text, nullable=False)                              # Markdown or structured content
    structured_data = Column(JSON, nullable=True)                       # Machine-readable data
    version = Column(Integer, default=1)
    parent_id = Column(Integer, nullable=True)                          # Self-FK for versioning chain
    agent_key = Column(String(100), nullable=True)                      # Which agent produced this
    message_id = Column(Integer, nullable=True)                         # Links to conversation turn
    file_path = Column(Text, nullable=True)                             # If exported to file
    file_name = Column(String(500), nullable=True)
    is_pinned = Column(Boolean, default=False)
    status = Column(String(30), default="draft")                        # draft, finalized
    metadata_json = Column(JSON, nullable=True)
    created_by = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("ix_artifacts_session_type", "session_id", "artifact_type"),
    )
