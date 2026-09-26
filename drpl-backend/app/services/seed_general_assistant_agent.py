"""DRPL Backend - Seed: General Assistant

Creates the `general_assistant` CustomAgent row so the Command Center's
conversational agent appears in Admin → Agent Builder like every other agent:
its prompt is editable, its toolbelt is visible, and both can be changed
without a deploy.

Its execution logic lives in
`app/services/langchain/graphs/general_assistant_agent.py`.

Run:  python -m app.services.seed_general_assistant_agent
"""

import logging

from app.core.database import SessionLocal
from app.models.agent_builder import AgentTool, CustomAgent
from app.services.agent_builder_service import create_agent
from app.services.langchain.graphs.general_assistant_agent import (
    AGENT_KEY,
    GENERAL_ASSISTANT_SYSTEM_PROMPT,
    GENERAL_ASSISTANT_TOOL_KEYS,
)

logger = logging.getLogger(__name__)

DESCRIPTION = (
    "The Command Center's general assistant. Handles anything that isn't a "
    "specialist job: web research with real citations, questions about a "
    "tender's stored documents and costings, drafting emails, letters and "
    "notes, ad-hoc calculations, and generating Word or Excel files. Can hand "
    "off to the specialist agents when the user wants a full analysis, "
    "costing, checklist or proposal produced."
)

AGENT_DATA = {
    "agent_key": AGENT_KEY,
    "display_name": "General Assistant",
    "description": DESCRIPTION,
    "category": "assistant",
    "agent_type": "react",
    "system_prompt": GENERAL_ASSISTANT_SYSTEM_PROMPT,
    "model": "claude-haiku-4-5",
    "provider": "anthropic",
    # Conversational, but it drafts prose a bid manager will send. Low enough
    # to keep facts stable, high enough that the writing isn't wooden.
    "temperature": 0.3,
    "max_tokens": 8192,
    "thinking_mode": "disabled",
    "effort": "medium",
    "tags": ["assistant", "chat", "research", "drafting", "general"],
    "is_system": True,
    "is_enabled": True,
    "is_published": True,
}


def _tool_rows(db) -> list[dict]:
    """The belt as Agent Builder stores it: [{"tool_id": N, "config": {}}].

    Resolved from `agent_tools` by key so the Agent Builder UI shows the real
    twelve rather than "Assigned Tools (0)" — the display that hid
    costing_researcher's missing toolbelt for months.
    """
    rows = (
        db.query(AgentTool)
        .filter(AgentTool.tool_key.in_(GENERAL_ASSISTANT_TOOL_KEYS))
        .all()
    )
    by_key = {r.tool_key: r.id for r in rows}
    missing = [k for k in GENERAL_ASSISTANT_TOOL_KEYS if k not in by_key]
    if missing:
        # Not fatal: the runtime loads by key, not by id. But the UI would be
        # lying about what the agent can do, which is worth a loud line.
        logger.warning(
            "[seed] general_assistant: no agent_tools row for %s — the Agent "
            "Builder tool list will be incomplete until seed_agent_tools runs.",
            missing,
        )
    return [
        {"tool_id": by_key[key], "config": {}}
        for key in GENERAL_ASSISTANT_TOOL_KEYS
        if key in by_key
    ]


def seed_general_assistant_agent(db=None):
    """Create or re-sync the general_assistant agent record. Idempotent."""
    close_db = False
    if db is None:
        db = SessionLocal()
        close_db = True

    try:
        existing = (
            db.query(CustomAgent).filter(CustomAgent.agent_key == AGENT_KEY).first()
        )

        if existing:
            existing.display_name = AGENT_DATA["display_name"]
            existing.description = AGENT_DATA["description"]
            existing.tags = AGENT_DATA["tags"]
            existing.is_published = True

            # Only re-sync prompt + tools while the admin hasn't customized
            # them, or every restart would silently discard their edits.
            if not existing.is_user_customized:
                existing.system_prompt = AGENT_DATA["system_prompt"]
                existing.tools = _tool_rows(db)
                logger.info(
                    "[seed] '%s' re-synced from code canonical", AGENT_KEY,
                )
            else:
                logger.info(
                    "[seed] '%s' is user-customized — preserving admin edits",
                    AGENT_KEY,
                )

            db.commit()
            return existing

        data = dict(AGENT_DATA)
        data["tools"] = _tool_rows(db)
        agent = create_agent(db, data, user_id=None)
        logger.info("[seed] created agent '%s' (id=%s)", AGENT_KEY, agent.id)
        return agent

    finally:
        if close_db:
            db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    seed_general_assistant_agent()
