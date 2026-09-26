"""Release agents from tool assignments that now narrow them.

This module previously did the opposite: it copied each agent's canonical
default tools into its `tools` column, because an empty column meant the agent
ran with no tools at all. `costing_researcher` was bound to nothing while its
registry declared eight, and the Agent Builder honestly reported "Assigned
Tools (0)".

That was a workaround for the wrong default. `tool_loader.resolve_agent_tool_keys`
now treats an empty column as "the whole shared repo", so an unconfigured agent
is fully capable rather than crippled. Against that rule, the lists the backfill
wrote are no longer a fix — they are a restriction, capping an agent at eight
tools when it could reach every one.

So this pass clears an assignment **only when it exactly matches that agent's
canonical default**, which is the fingerprint of the backfill having written it.
An assignment a human chose differs from the canonical list and is never
touched: narrowing an agent on purpose has to keep working, or the shared repo
becomes a rule with no exceptions rather than a default with one.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.agent_builder import AgentTool, CustomAgent
from app.services.langchain.canonical_registry import CANONICAL_AGENTS

logger = logging.getLogger(__name__)


def _keys_of(db: Session, tools: list) -> set[str]:
    """The tool_keys behind an agent's stored `tools` entries."""
    ids = [
        entry.get("tool_id") if isinstance(entry, dict) else entry
        for entry in tools or []
    ]
    ids = [i for i in ids if isinstance(i, int)]
    if not ids:
        return set()
    return {
        row.tool_key
        for row in db.query(AgentTool.tool_key).filter(AgentTool.id.in_(ids)).all()
    }


def seed_agent_tools(db: Session) -> dict:
    """Clear canonical-default assignments so those agents reach the shared repo.

    Returns ``{"released": N, "changes": [...]}``.
    """
    if not get_settings().agent_tool_release_enabled:
        logger.info("[DRPL] Agent tool release disabled; skipping.")
        return {"released": 0, "changes": [], "skipped": True}

    changes: list[dict] = []

    for agent_key, spec in CANONICAL_AGENTS.items():
        defaults = set(spec.get("default_tools") or [])
        if not defaults:
            continue

        agent = (
            db.query(CustomAgent).filter(CustomAgent.agent_key == agent_key).first()
        )
        if not agent or not agent.tools:
            continue

        if _keys_of(db, agent.tools) != defaults:
            continue  # a human's choice, not the backfill's

        agent.tools = []
        db.add(agent)
        changes.append({"agent_key": agent_key, "released_from": sorted(defaults)})

    if changes:
        db.commit()
        for c in changes:
            logger.info(
                "[DRPL] Agent tool release: %s freed from its %d-tool canonical "
                "list — it now reaches the shared repo.",
                c["agent_key"], len(c["released_from"]),
            )
    logger.info("[DRPL] Agent tool release: %d agents freed.", len(changes))
    return {"released": len(changes), "changes": changes}
