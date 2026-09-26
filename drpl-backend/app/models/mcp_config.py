"""
DRPL Backend - MCP Server Configuration Model
Stores external MCP server connection configurations.
"""

from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, DateTime, Text, Boolean, JSON
from app.core.database import Base


class MCPServerConfig(Base):
    """Configuration for connecting to external MCP servers."""
    __tablename__ = "mcp_server_configs"

    id = Column(Integer, primary_key=True, index=True)
    server_name = Column(String(100), unique=True, nullable=False)
    display_name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    transport_type = Column(String(50), nullable=False)              # stdio, sse, http
    command = Column(String(500), nullable=True)                     # For stdio: command to run
    args = Column(JSON, default=list)                                # For stdio: command arguments
    url = Column(String(500), nullable=True)                         # For SSE/HTTP: server URL
    env_vars = Column(JSON, default=dict)                            # Environment variables to pass
    is_enabled = Column(Boolean, default=True)
    created_by = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))
