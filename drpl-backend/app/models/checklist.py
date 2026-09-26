"""
DRPL Backend - Checklist Model
Tracks required documents for each tender submission
"""

from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Text, Boolean, DateTime
from app.core.database import Base


class ChecklistItem(Base):
    __tablename__ = "checklist_items"

    id = Column(Integer, primary_key=True, index=True)
    tender_id = Column(Integer, nullable=False, index=True)         # FK to tenders.id
    item_name = Column(String(500), nullable=False)                  # e.g., "Company Registration Certificate"
    item_description = Column(Text, nullable=True)
    is_required = Column(Boolean, default=True)
    is_uploaded = Column(Boolean, default=False)
    document_id = Column(Integer, nullable=True)                     # FK to tender_documents.id once uploaded
    display_order = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    # --- Automation fields ---
    item_category = Column(String(30), default="standard", nullable=False)  # "standard" | "generated" | "analysis"
    generation_status = Column(String(30), default="pending", nullable=False)  # "pending"|"queued"|"generating"|"generated"|"review"|"approved"|"failed"
    generated_document_id = Column(Integer, nullable=True)           # FK to generated_documents.id once created
    generation_error = Column(Text, nullable=True)                   # error message if generation failed
    ai_instructions = Column(Text, nullable=True)                    # specific AI prompt for generating this document
    source_section = Column(Text, nullable=True)                     # which tender section this requirement came from

    # --- Workspace fields ---
    is_not_required = Column(Boolean, default=False)                 # User marks standard docs as not needed
    workspace_status = Column(String(30), default="not_started")     # Mirrors DocumentWorkspace.review_status for fast queries
    agent_key = Column(String(100), nullable=True)                   # Assigned agent for workspace generation
