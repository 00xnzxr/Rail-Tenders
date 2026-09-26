"""
DRPL Backend - Redaction Rule Model
Configurable PII/sensitive data patterns
"""

from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Text, Boolean, DateTime
from app.core.database import Base


class RedactionRule(Base):
    __tablename__ = "redaction_rules"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    pattern = Column(Text, nullable=False)
    replacement = Column(String(255), nullable=False)
    category = Column(String(100), nullable=False)       # pii, financial, custom
    is_enabled = Column(Boolean, default=True)
    is_system = Column(Boolean, default=False)            # system rules cannot be deleted
    description = Column(Text, nullable=True)
    created_by = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
