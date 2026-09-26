"""
DRPL Backend - Agent Configuration Model
Per-agent AI settings stored in database
"""

from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Text, Float, Boolean, DateTime
from app.core.database import Base


class AgentConfig(Base):
    __tablename__ = "agent_configs"

    id = Column(Integer, primary_key=True, index=True)
    agent_name = Column(String(100), unique=True, nullable=False)
    display_name = Column(String(255), nullable=False)
    is_enabled = Column(Boolean, default=True)
    ai_provider = Column(String(50), nullable=True)      # override per-agent
    ai_model = Column(String(255), nullable=True)          # override per-agent
    temperature = Column(Float, default=0.7)
    max_tokens = Column(Integer, default=1024)
    system_prompt_override = Column(Text, nullable=True)
    description = Column(Text, nullable=True)

    # Extended Thinking & Effort
    thinking_mode = Column(String(20), nullable=True)          # "auto", "adaptive", "enabled", "disabled"
    thinking_budget_tokens = Column(Integer, nullable=True)    # Token budget for manual extended thinking
    effort = Column(String(20), nullable=True)                 # "low", "medium", "high", "max"

    updated_by = Column(Integer, nullable=True)
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
