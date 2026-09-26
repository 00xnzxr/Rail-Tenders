"""
DRPL Backend - API Token Model
Stores hashed API tokens for extension authentication
"""

from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, DateTime, Boolean
from app.core.database import Base


class APIToken(Base):
    __tablename__ = "api_tokens"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, nullable=False, index=True)
    token_hash = Column(String(255), nullable=False, unique=True, index=True)
    name = Column(String(100), nullable=False)
    prefix = Column(String(20), nullable=False)  # first 12 chars for display
    last_used_at = Column(DateTime(timezone=True), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
