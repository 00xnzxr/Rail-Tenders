"""
DRPL Backend - Agent Configuration Service
Manages per-agent AI settings
"""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.models.agent_config import AgentConfig

DEFAULT_AGENTS = [
    {"agent_name": "classifier", "display_name": "Tender Classifier", "description": "Categorizes tenders into Mechanical, Electrical, Civil, IT/Software, Materials/Supplies, Consulting, Other", "max_tokens": 256, "temperature": 0.3},
    {"agent_name": "relevance", "display_name": "Relevance Scorer", "description": "Scores tender relevance to DRPL Railway mechanical/electrical capabilities (0-1)", "max_tokens": 256, "temperature": 0.3},
    {"agent_name": "risk", "display_name": "Risk Assessor", "description": "Assesses tender risk factors including deadlines, EMD, scope clarity (0-1)", "max_tokens": 256, "temperature": 0.3},
    {"agent_name": "summary", "display_name": "Tender Summarizer", "description": "Generates 2-3 sentence executive summaries of tender scope and requirements", "max_tokens": 512, "temperature": 0.5},
    {"agent_name": "eligibility", "display_name": "Eligibility Checker", "description": "AI-based eligibility assessment checking DRPL qualifications against tender requirements", "max_tokens": 512, "temperature": 0.4},
    {"agent_name": "checklist", "display_name": "Checklist Generator", "description": "Extracts required documents and compliance items from tender specifications", "max_tokens": 2048, "temperature": 0.3},
    {"agent_name": "proposal", "display_name": "Proposal Writer", "description": "Streaming chat agent specialized in Indian Railways tender proposal creation (IREPS, GeM, CPPP)", "max_tokens": 4096, "temperature": 0.7},
    {"agent_name": "document_analyzer", "display_name": "Document Analyzer", "description": "Deep extraction of requirements, eligibility, terms, critical clauses from tender documents with OCR support", "max_tokens": 4096, "temperature": 0.2},
]


def seed_agents(db: Session):
    """Seed default agent configs if they don't exist."""
    for default in DEFAULT_AGENTS:
        existing = db.query(AgentConfig).filter(AgentConfig.agent_name == default["agent_name"]).first()
        if not existing:
            agent = AgentConfig(
                agent_name=default["agent_name"],
                display_name=default["display_name"],
                description=default.get("description", ""),
                max_tokens=default.get("max_tokens", 1024),
                temperature=default.get("temperature", 0.7),
            )
            db.add(agent)
    db.commit()


def list_agents(db: Session) -> list[AgentConfig]:
    """Get all agent configs."""
    return db.query(AgentConfig).order_by(AgentConfig.agent_name).all()


def get_agent_config(db: Session, agent_name: str) -> Optional[AgentConfig]:
    """Get a single agent config."""
    return db.query(AgentConfig).filter(AgentConfig.agent_name == agent_name).first()


def update_agent_config(db: Session, agent_name: str, updates: dict, updated_by: int) -> Optional[AgentConfig]:
    """Update an agent config."""
    agent = get_agent_config(db, agent_name)
    if not agent:
        return None

    for key, value in updates.items():
        if hasattr(agent, key) and key not in ("id", "agent_name"):
            setattr(agent, key, value)

    agent.updated_by = updated_by
    agent.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(agent)
    return agent


def reset_agent_config(db: Session, agent_name: str, updated_by: int) -> Optional[AgentConfig]:
    """Reset an agent to defaults."""
    for default in DEFAULT_AGENTS:
        if default["agent_name"] == agent_name:
            return update_agent_config(db, agent_name, {
                "is_enabled": True,
                "ai_provider": None,
                "ai_model": None,
                "temperature": default.get("temperature", 0.7),
                "max_tokens": default.get("max_tokens", 1024),
                "system_prompt_override": None,
            }, updated_by)
    return None
