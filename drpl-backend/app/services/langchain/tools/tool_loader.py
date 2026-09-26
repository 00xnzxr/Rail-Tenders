"""
DRPL Backend - LangChain Tool Loader
Dynamically loads LangChain BaseTool instances for a given agent
based on the agent's tool configuration and the AgentTool registry.
"""

import logging
from typing import Optional

from langchain_core.tools import BaseTool
from sqlalchemy.orm import Session

from app.models.agent_builder import CustomAgent, AgentTool

logger = logging.getLogger(__name__)

# Registry mapping tool_key to LangChain tool class
_TOOL_CLASS_REGISTRY: dict[str, type] = {}


def _ensure_registry():
    """Populate the tool class registry from the capability registry.

    This used to be a hand-maintained import block and dict literal — the
    first of the four divergent catalogs. It is now derived: a `kind="class"`
    capability added to the registry is loadable here with no second edit,
    and `test_loader_registry_matches_the_capability_registry` pins the two
    sets equal so they can never quietly drift apart again.
    """
    if _TOOL_CLASS_REGISTRY:
        return

    import importlib

    from app.services.langchain.capability_registry import CAPABILITIES

    for key, cap in CAPABILITIES.items():
        if cap.kind != "class":
            continue
        module_path, attr = cap.target.split(":")
        try:
            module = importlib.import_module(module_path)
            _TOOL_CLASS_REGISTRY[key] = getattr(module, attr)
        except Exception as e:
            # A broken import must not take down every other tool with it —
            # but it must be loud: this capability is now silently missing.
            logger.error("Could not load tool class for '%s' (%s): %s", key, cap.target, e)


#: Agents that make one short, structured call and genuinely need no tools —
#: scoring a tender 0-1, picking a category, writing a two-sentence summary,
#: choosing a route. They are exempt from the shared-repo default below.
#:
#: This is not tidiness. Auto-scoring runs 750 agent calls an hour, and the
#: full tool catalog is roughly 7,900 input tokens of schema. Binding it to
#: these five would add ~$142/day for definitions they would never call, and
#: large tool sets measurably degrade tool selection for the agents that do
#: need to choose.
TOOL_FREE_AGENTS = {
    "relevance",
    "risk",
    "classifier",
    "summary",
    "eligibility",
    "proposal_router",
    "costing_scope_extractor",
}


def _wants_db(tool_cls: type) -> bool:
    """Does this tool take a database session?

    `hasattr(tool_cls, "db")` was the obvious way to ask, and it is wrong.
    These tools are Pydantic v2 models, where a declared field lives in
    `model_fields` and is NOT a class attribute — so the check returned False
    for every tool in the registry and every one of them was constructed with
    `db=None`.

    Nothing crashed, which is why it survived: the tools treat a missing
    session as "no database available" and degrade. `web_search` degraded by
    reading its API keys from `.env` only, never from PlatformSetting, so a
    platform whose keys live in the database fell past Gemini grounding and
    Tavily to DuckDuckGo on every single call.
    """
    fields = getattr(tool_cls, "model_fields", None)
    if fields and "db" in fields:
        return True
    return hasattr(tool_cls, "db")


def resolve_agent_tool_keys(agent_key: Optional[str], configured: list) -> Optional[list[str]]:
    """Which tool keys an agent should get.

    Returns None when the agent's own configured list should be used as-is.

    The default is inverted from what it used to be. An empty `tools` column
    once meant "no tools", which is how costing_researcher — an agent whose
    entire job is web research and rate lookup — ended up bound to nothing at
    all while the UI honestly reported "Assigned Tools (0)". Empty now means
    "the whole repo": an agent is fully capable unless someone deliberately
    narrows it, and the failure mode of forgetting to configure something is a
    capable agent rather than a crippled one.
    """
    if configured:
        return None  # an explicit choice always wins
    if agent_key in TOOL_FREE_AGENTS:
        return []
    return get_available_tool_keys()


def _apply_policy(tools: list[BaseTool], db: Optional[Session] = None) -> list[BaseTool]:
    """Gate write tools before any agent can call them, and recycle the session.

    Applied here, at the single choke point every specialist agent loads its
    tools through, rather than at each of the call sites inside
    `chat_agent_wrappers`. Without this the router's direct-to-specialist path
    writes with no confirmation, while the orchestrator path is gated — one
    safety model with a hole in it is worse than none, because it is the hole
    nobody remembers.

    Reads are returned untouched by the gate, so it is a no-op for most tools.

    The session recycler goes on the OUTSIDE, so it also runs when the gate
    suspends a write. It ends the run's open read transaction after every call,
    which is what stops Neon's `idle_in_transaction_session_timeout` from
    killing a connection mid-run — see `db_recycle`. It is deliberately outside
    the gate's `enabled` switch: turning the confirmation gate off must not
    quietly turn connection hygiene off with it.
    """
    from app.core.config import get_settings
    from app.services.langchain.db_recycle import recycle_session_between_calls
    from app.services.langchain.tool_policy import wrap_tools_with_policy

    gated = wrap_tools_with_policy(
        tools,
        capture=None,  # binds late to the run's open policy_scope
        enabled=get_settings().assistant_confirm_gate_enabled,
    )
    return recycle_session_between_calls(gated, db)


def load_tools_for_agent(
    db: Session,
    agent: CustomAgent,
    agent_key_for_memory: Optional[str] = None,
    proposal_session_id: Optional[int] = None,
    router_session_id: Optional[str] = None,
) -> list[BaseTool]:
    """
    Load LangChain BaseTool instances for an agent based on its tools config.

    The agent's `tools` field is a JSON list like:
      [{"tool_id": 1, "config": {...}}, {"tool_id": 2, "config": {...}}]

    Each tool_id maps to an AgentTool record with a tool_key that maps
    to a Python class in the registry.

    Args:
        db: Database session (injected into tools that need it)
        agent: The CustomAgent whose tools to load
        agent_key_for_memory: Agent key to scope memory operations (defaults to agent.agent_key)

    Returns:
        List of configured LangChain BaseTool instances
    """
    _ensure_registry()

    tools_config = agent.tools or []
    if not tools_config:
        # Empty means the shared repo, not nothing. See resolve_agent_tool_keys.
        shared = resolve_agent_tool_keys(agent.agent_key, tools_config)
        if not shared:
            return []
        logger.info(
            "Agent '%s' has no explicit tool assignment — binding the shared "
            "repo (%d tools).", agent.agent_key, len(shared),
        )
        return load_tools_by_keys(
            db, shared,
            agent_key=agent_key_for_memory or agent.agent_key,
            proposal_session_id=proposal_session_id,
            router_session_id=router_session_id,
        )

    loaded_tools = []
    memory_agent_key = agent_key_for_memory or agent.agent_key

    for tool_entry in tools_config:
        tool_id = tool_entry.get("tool_id") if isinstance(tool_entry, dict) else tool_entry
        config = tool_entry.get("config", {}) if isinstance(tool_entry, dict) else {}

        # Look up the tool in the registry
        agent_tool = db.query(AgentTool).filter(AgentTool.id == tool_id).first()
        if not agent_tool:
            logger.warning(f"AgentTool with ID {tool_id} not found, skipping")
            continue

        if not agent_tool.is_active:
            logger.info(f"Tool '{agent_tool.tool_key}' is inactive, skipping")
            continue

        tool_key = agent_tool.tool_key

        if tool_key in _TOOL_CLASS_REGISTRY:
            tool_cls = _TOOL_CLASS_REGISTRY[tool_key]
            try:
                # Instantiate with injected dependencies
                kwargs = {}

                # Most tools need a DB session
                if _wants_db(tool_cls):
                    kwargs["db"] = db

                # Memory tools need agent_key scoping
                if tool_key in ("memory_store", "memory_retrieve"):
                    kwargs["agent_key"] = memory_agent_key

                # Clarify tool needs session + agent scoping
                if tool_key == "clarify":
                    kwargs["session_id"] = proposal_session_id
                    kwargs["router_session_id"] = router_session_id
                    kwargs["agent_key"] = agent.agent_key

                # Bound by the run, never named by the model.
                if tool_key == "conversation_history_search":
                    kwargs["router_session_id"] = router_session_id

                tool_instance = tool_cls(**kwargs)
                loaded_tools.append(tool_instance)
                logger.debug(f"Loaded tool: {tool_key}")

            except Exception as e:
                logger.error(f"Failed to instantiate tool '{tool_key}': {e}")
        else:
            logger.warning(f"No LangChain implementation for tool_key '{tool_key}', skipping")

    return _apply_policy(loaded_tools, db)


def load_tools_by_keys(
    db: Session,
    tool_keys: list[str],
    agent_key: Optional[str] = None,
    proposal_session_id: Optional[int] = None,
    router_session_id: Optional[str] = None,
) -> list[BaseTool]:
    """
    Load tools directly by their tool_key strings (convenience method).
    Useful when building agents programmatically without a CustomAgent record.
    """
    _ensure_registry()

    loaded = []
    for key in tool_keys:
        if key in _TOOL_CLASS_REGISTRY:
            tool_cls = _TOOL_CLASS_REGISTRY[key]
            kwargs = {}
            if _wants_db(tool_cls):
                kwargs["db"] = db
            if key in ("memory_store", "memory_retrieve"):
                kwargs["agent_key"] = agent_key
            if key == "clarify":
                kwargs["session_id"] = proposal_session_id
                kwargs["router_session_id"] = router_session_id
                kwargs["agent_key"] = agent_key
            if key == "conversation_history_search":
                kwargs["router_session_id"] = router_session_id
            try:
                loaded.append(tool_cls(**kwargs))
            except Exception as e:
                logger.error(f"Failed to instantiate tool '{key}': {e}")
        else:
            logger.warning(f"Unknown tool_key: {key}")

    return _apply_policy(loaded, db)


def get_available_tool_keys() -> list[str]:
    """Return all registered tool keys."""
    _ensure_registry()
    return list(_TOOL_CLASS_REGISTRY.keys())
