"""
DRPL Backend - API Usage Log Model
Tracks AI API calls for cost monitoring
"""

from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Text, Float, Boolean, DateTime
from app.core.database import Base


class APIUsageLog(Base):
    __tablename__ = "api_usage_logs"

    id = Column(Integer, primary_key=True, index=True)
    provider = Column(String(50), nullable=False)
    model = Column(String(255), nullable=False)
    agent_name = Column(String(100), nullable=True)
    tokens_input = Column(Integer, default=0)
    tokens_output = Column(Integer, default=0)
    cost_estimate = Column(Float, default=0.0)
    user_id = Column(Integer, nullable=True)
    # Which agent run this call belongs to. Stamped from `run_id_scope`, the
    # same ambient scope that stamps `[run=...]` onto log lines, so no call
    # site has to pass it. NULL for spend outside any run — seeders, the
    # archive sweep, scheduled jobs.
    run_id = Column(String(64), nullable=True, index=True)
    success = Column(Boolean, default=True)
    error_message = Column(Text, nullable=True)
    response_time_ms = Column(Integer, nullable=True)
    # Prompt caching metrics
    cache_read_tokens = Column(Integer, default=0)
    cache_creation_tokens = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True)
