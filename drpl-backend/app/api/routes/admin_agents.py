"""
DRPL Backend - Admin Agent Configuration Routes
AI agent settings management (master_admin only)
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional

from app.core.database import get_db
from app.core.auth import require_master_admin
from app.models.user import User
from app.services.audit_service import log_action
from app.services.agent_config_service import (
    list_agents, get_agent_config, update_agent_config, reset_agent_config, seed_agents,
)

router = APIRouter(prefix="/admin/agents", tags=["admin-agents"])


class AgentConfigResponse(BaseModel):
    id: int
    agent_name: str
    display_name: str
    is_enabled: bool
    ai_provider: Optional[str]
    ai_model: Optional[str]
    temperature: float
    max_tokens: int
    system_prompt_override: Optional[str]
    description: Optional[str]
    updated_by: Optional[int]
    updated_at: Optional[str]

    class Config:
        from_attributes = True


class UpdateAgentInput(BaseModel):
    display_name: Optional[str] = None
    is_enabled: Optional[bool] = None
    ai_provider: Optional[str] = None
    ai_model: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    system_prompt_override: Optional[str] = None
    description: Optional[str] = None


@router.get("/", response_model=list[AgentConfigResponse])
def list_agent_configs(
    db: Session = Depends(get_db),
    admin: User = Depends(require_master_admin),
):
    """List all AI agent configurations."""
    seed_agents(db)
    agents = list_agents(db)
    return [AgentConfigResponse(
        id=a.id, agent_name=a.agent_name, display_name=a.display_name,
        is_enabled=a.is_enabled, ai_provider=a.ai_provider, ai_model=a.ai_model,
        temperature=a.temperature, max_tokens=a.max_tokens,
        system_prompt_override=a.system_prompt_override, description=a.description,
        updated_by=a.updated_by,
        updated_at=a.updated_at.isoformat() if a.updated_at else None,
    ) for a in agents]


@router.get("/{agent_name}", response_model=AgentConfigResponse)
def get_agent(
    agent_name: str,
    db: Session = Depends(get_db),
    admin: User = Depends(require_master_admin),
):
    """Get a single agent configuration."""
    seed_agents(db)
    agent = get_agent_config(db, agent_name)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    return AgentConfigResponse(
        id=agent.id, agent_name=agent.agent_name, display_name=agent.display_name,
        is_enabled=agent.is_enabled, ai_provider=agent.ai_provider, ai_model=agent.ai_model,
        temperature=agent.temperature, max_tokens=agent.max_tokens,
        system_prompt_override=agent.system_prompt_override, description=agent.description,
        updated_by=agent.updated_by,
        updated_at=agent.updated_at.isoformat() if agent.updated_at else None,
    )


@router.put("/{agent_name}", response_model=AgentConfigResponse)
def update_agent(
    agent_name: str,
    body: UpdateAgentInput,
    db: Session = Depends(get_db),
    admin: User = Depends(require_master_admin),
):
    """Update an agent configuration."""
    updates = body.model_dump(exclude_none=True)
    agent = update_agent_config(db, agent_name, updates, admin.id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    log_action(db, admin.id, admin.email, "agent.updated", "agent", agent_name, updates)
    return AgentConfigResponse(
        id=agent.id, agent_name=agent.agent_name, display_name=agent.display_name,
        is_enabled=agent.is_enabled, ai_provider=agent.ai_provider, ai_model=agent.ai_model,
        temperature=agent.temperature, max_tokens=agent.max_tokens,
        system_prompt_override=agent.system_prompt_override, description=agent.description,
        updated_by=agent.updated_by,
        updated_at=agent.updated_at.isoformat() if agent.updated_at else None,
    )


@router.post("/{agent_name}/reset")
def reset_agent(
    agent_name: str,
    db: Session = Depends(get_db),
    admin: User = Depends(require_master_admin),
):
    """Reset agent to default configuration."""
    result = reset_agent_config(db, agent_name, admin.id)
    if not result:
        raise HTTPException(status_code=404, detail="Agent not found")
    log_action(db, admin.id, admin.email, "agent.reset", "agent", agent_name)
    return {"status": "reset", "agent_name": agent_name}
