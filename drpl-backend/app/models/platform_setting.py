"""
DRPL Backend - Platform Settings Model
Runtime-configurable settings stored in database
"""

from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Text, Boolean, DateTime
from app.core.database import Base


class PlatformSetting(Base):
    __tablename__ = "platform_settings"

    id = Column(Integer, primary_key=True, index=True)
    key = Column(String(255), unique=True, nullable=False, index=True)
    value = Column(Text, nullable=False)                # JSON-encoded value
    value_type = Column(String(50), nullable=False)      # string, int, float, bool, json
    category = Column(String(100), nullable=False)       # general, ai, security, notifications
    description = Column(Text, nullable=True)
    is_secret = Column(Boolean, default=False)
    updated_by = Column(Integer, nullable=True)
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
