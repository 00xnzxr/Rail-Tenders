"""
DRPL Backend - Chat Attachment Model
Tracks files uploaded to Command Center chat sessions.
"""

from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Text, DateTime

from app.core.database import Base


class ChatAttachment(Base):
    __tablename__ = "chat_attachments"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, nullable=False, index=True)       # FK to proposal_sessions.id
    message_id = Column(Integer, nullable=True)                     # FK to proposal_messages.id (set after message saved)
    file_name = Column(String(500), nullable=False)                 # Original filename
    file_path = Column(Text, nullable=False)                        # Disk path
    file_type = Column(String(100), nullable=True)                  # MIME type
    file_size = Column(Integer, nullable=True)                      # Bytes
    uploaded_by = Column(Integer, nullable=False)                   # FK to users.id
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    # Linked document tracking
    parent_attachment_id = Column(Integer, nullable=True, index=True)  # FK to chat_attachments.id (self-ref)
    source_url = Column(Text, nullable=True)                           # URL this was downloaded from
    extraction_status = Column(String(30), default="pending")          # pending, processing, completed, failed, skipped
