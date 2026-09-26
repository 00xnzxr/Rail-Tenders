"""
DRPL Backend - Data Retention Policy Model
Configurable data retention for different data types
"""

from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Text, Boolean, DateTime
from app.core.database import Base


class DataRetentionPolicy(Base):
    __tablename__ = "data_retention_policies"

    id = Column(Integer, primary_key=True, index=True)
    data_type = Column(String(100), unique=True, nullable=False)
    retention_days = Column(Integer, nullable=False, default=0)  # 0 = keep forever
    auto_delete = Column(Boolean, default=False)
    is_enabled = Column(Boolean, default=True)
    description = Column(Text, nullable=True)
    updated_by = Column(Integer, nullable=True)
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
