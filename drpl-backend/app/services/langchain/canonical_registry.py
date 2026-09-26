"""
DRPL Backend — Canonical Agent Registry (Phase 3d)

Single source of truth for each system agent's:
  - canonical (code-defined) system prompt
  - canonical default tool list
  - required placeholders that must be preserved if the user customizes the prompt
  - whether the agent's prompt / tools are user-editable at all

Purpose: enable Agent Builder UI edits to flow through to the runtime safely.
The runtime resolver (`resolve_system_prompt` / `resolve_tool_keys`) consults
`CustomAgent.system_prompt` + `.tools` first; falls back to canonical when the
agent has not been user-customized OR when the user's prompt fails validation
(e.g. removed a required placeholder). This makes the platform admin-managed
without engineering involvement, while keeping a fail-safe so a broken edit
never bricks an agent.

Registry shape per agent:

  {
    "prompt_constant_path": "module.path:ATTR_NAME",  # for fetch / reset
    "default_tools": [...],                            # tool_keys list
    "required_placeholders": [...],                    # e.g. "<gst_percent>"
    "soft_placeholders": [...],                        # warn, don't block
    "supports_user_prompt": bool,                      # False = system-managed
    "supports_user_tools": bool,                       # False = system-managed
  }
"""

from __future__ import annotations

import hashlib
import importlib
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

CANONICAL_AGENTS: dict[str, dict[str, Any]] = {
    "costing_researcher": {
        "prompt_constant_path": "app.services.langchain.graphs.costing_agent:COSTING_AGENT_SYSTEM_PROMPT",
        "default_tools": [
            "memory_store",
            "memory_retrieve",
            "ratecard_lookup",
            "costing_training_retrieval",
            "anonymizing_web_search",
            "delegate_travel_research",
            "clarify",
            "cost_calculator",
        ],
        "required_placeholders": [
            "<overhead_percent>",
            "<margin_percent>",
            "<gst_percent>",
        ],
        # Soft markers — agent's structured-output contract. Missing them
        # produces warnings (output may not parse) but doesn't block save.
        "soft_placeholders": ["COSTING_JSON_START", "COSTING_JSON_END"],
        "supports_user_prompt": True,
        "supports_user_tools": True,
    },
    "tender_doc_analyzer": {
        # Also resolved via agent_key="deep_analyzer" by some call sites — both
        # alias to this entry through the alias map below.
        "prompt_constant_path": "app.services.langchain.graphs.document_analysis_agent:DOCUMENT_ANALYSIS_SYSTEM_PROMPT",
        "default_tools": [
            "document_reader",
            "semantic_search",
            "web_search",
            "memory_retrieve",
            "memory_store",
            "tender_lookup",
        ],
        "required_placeholders": [],
        "soft_placeholders": [],
        "supports_user_prompt": True,
        "supports_user_tools": True,
    },
    "annexure_finder": {
        # Pass 1 of two-pass extraction: LOCATE each form (identifier/title/pages).
        # The body transcription lives under "annexure_transcriber" below.
        "prompt_constant_path": "app.services.langchain.graphs.annexure_finder_agent:ANNEXURE_DISCOVERY_PROMPT",
        "default_tools": [],  # Vision-only path; no LangChain tools
        "required_placeholders": [],
        "soft_placeholders": [],
        "supports_user_prompt": True,
        "supports_user_tools": False,  # vision-direct, no tool registry
    },
    "annexure_transcriber": {
        # Pass 2: verbatim transcription of ONE annexure from its own pages.
        # Split from annexure_finder so a customization of one pass cannot break
        # the other, and so the strict no-paraphrase rules stay editable alone.
        "prompt_constant_path": "app.services.langchain.graphs.annexure_finder_agent:ANNEXURE_TRANSCRIPTION_PROMPT",
        "default_tools": [],
        "required_placeholders": [],
        "soft_placeholders": [],
        "supports_user_prompt": True,
        "supports_user_tools": False,
    },
    "proposal_creator": {
        "prompt_constant_path": "app.services.langchain.graphs.proposal_agent:PROPOSAL_AGENT_SYSTEM_PROMPT",
        "default_tools": [
            "document_generator",
            "web_search",
            "memory_retrieve",
            "memory_store",
        ],
        "required_placeholders": [],
        "soft_placeholders": [],
        "supports_user_prompt": True,
        "supports_user_tools": True,
    },
    "proposal_router": {
        "prompt_constant_path": "app.services.langchain.graphs.agent_router_graph:INTENT_CLASSIFIER_PROMPT",
        "default_tools": [],
        "required_placeholders": [],
        "soft_placeholders": [],
        # Router's output contract is fragile (intent enum + agent_key list);
        # editing risks breaking routing. Keep system-managed for now.
        "supports_user_prompt": False,
        "supports_user_tools": False,
    },
    "general_assistant": {
        # The conversational path. Everything the classifier cannot place as a
        # specialist job lands here, so its capability ceiling is the platform's
        # answer to "can I just ask it something?".
        "prompt_constant_path": (
            "app.services.langchain.graphs.general_assistant_agent:"
            "GENERAL_ASSISTANT_SYSTEM_PROMPT"
        ),
        "default_tools": [
            "web_search",
            "web_fetch",
            "document_reader",
            "tender_lookup",
            "semantic_search",
            "conversation_history_search",
            "ratecard_lookup",
            "cost_calculator",
            "memory_store",
            "memory_retrieve",
            "clarify",
            "docx_generator",
            "xlsx_generator",
        ],
        "required_placeholders": [],
        "soft_placeholders": [],
        "supports_user_prompt": True,
        "supports_user_tools": True,
    },
    "decision_maker": {
        "prompt_constant_path": None,  # built dynamically from sub-agent set
        "default_tools": [],  # dynamic — wraps every other system agent
        "required_placeholders": [],
        "soft_placeholders": [],
        "supports_user_prompt": False,  # orchestrator; system-managed
        "supports_user_tools": False,
    },
    # Workspace document agents — chain-of-thought, single LLM call. Their
    # prompts live inline in seed_document_agents.py and ARE read from the
    # CustomAgent.system_prompt at runtime by langchain_execution_service
    # (the workspace generator goes through get_chat_model_for_custom_agent),
    # so they already partially work. Register them here so reset-to-default
    # and validation work uniformly.
    "doc-letter-writer": {
        "prompt_constant_path": "app.services.seed_document_agents:DOCUMENT_AGENTS",
        "prompt_constant_extractor": ("doc-letter-writer", "system_prompt"),
        "default_tools": [],
        "required_placeholders": [],
        "soft_placeholders": [],
        "supports_user_prompt": True,
        "supports_user_tools": False,
    },
    "doc-technical-writer": {
        "prompt_constant_path": "app.services.seed_document_agents:DOCUMENT_AGENTS",
        "prompt_constant_extractor": ("doc-technical-writer", "system_prompt"),
        "default_tools": [],
        "required_placeholders": [],
        "soft_placeholders": [],
        "supports_user_prompt": True,
        "supports_user_tools": False,
    },
    "doc-costing-analyst": {
        "prompt_constant_path": "app.services.seed_document_agents:DOCUMENT_AGENTS",
        "prompt_constant_extractor": ("doc-costing-analyst", "system_prompt"),
        "default_tools": [],
        "required_placeholders": [],
        "soft_placeholders": [],
        "supports_user_prompt": True,
        "supports_user_tools": False,
    },
    "doc-compliance-writer": {
        "prompt_constant_path": "app.services.seed_document_agents:DOCUMENT_AGENTS",
        "prompt_constant_extractor": ("doc-compliance-writer", "system_prompt"),
        "default_tools": [],
        "required_placeholders": [],
        "soft_placeholders": [],
        "supports_user_prompt": True,
        "supports_user_tools": False,
    },
}

# Alias table — call sites use multiple agent_keys for the same logical agent.
# Resolve all aliases to the canonical primary key before registry lookup.
_AGENT_KEY_ALIASES: dict[str, str] = {
    "deep_analyzer": "tender_doc_analyzer",
}


def _canonical_key(agent_key: str) -> str:
    """Resolve aliases (e.g. 'deep_analyzer' → 'tender_doc_analyzer')."""
    return _AGENT_KEY_ALIASES.get(agent_key, agent_key)


# ---------------------------------------------------------------------------
# Public registry accessors
# ---------------------------------------------------------------------------

def is_registered(agent_key: str) -> bool:
    return _canonical_key(agent_key) in CANONICAL_AGENTS


def supports_user_prompt(agent_key: str) -> bool:
    entry = CANONICAL_AGENTS.get(_canonical_key(agent_key))
    return bool(entry and entry.get("supports_user_prompt"))


def supports_user_tools(agent_key: str) -> bool:
    entry = CANONICAL_AGENTS.get(_canonical_key(agent_key))
    return bool(entry and entry.get("supports_user_tools"))


def get_canonical_tools(agent_key: str) -> list[str]:
    entry = CANONICAL_AGENTS.get(_canonical_key(agent_key))
    if not entry:
        return []
    return list(entry.get("default_tools") or [])


def get_required_placeholders(agent_key: str) -> list[str]:
    entry = CANONICAL_AGENTS.get(_canonical_key(agent_key))
    if not entry:
        return []
    return list(entry.get("required_placeholders") or [])


def get_soft_placeholders(agent_key: str) -> list[str]:
    entry = CANONICAL_AGENTS.get(_canonical_key(agent_key))
    if not entry:
        return []
    return list(entry.get("soft_placeholders") or [])


def get_canonical_prompt(agent_key: str) -> Optional[str]:
    """Import the code-canonical prompt constant for an agent.

    Returns None when the agent has no single-string canonical (e.g. the
    decision_maker assembles its prompt at runtime from multiple sources).
    """
    entry = CANONICAL_AGENTS.get(_canonical_key(agent_key))
    if not entry:
        return None
    path = entry.get("prompt_constant_path")
    if not path:
        return None
    extractor = entry.get("prompt_constant_extractor")

    try:
        module_path, attr = path.split(":", 1)
        module = importlib.import_module(module_path)
        value = getattr(module, attr, None)
        if value is None:
            return None
        # Some agents store the canonical prompt inside a list-of-dicts
        # (DOCUMENT_AGENTS in seed_document_agents.py). The
        # `prompt_constant_extractor` tuple says (match_key, field_name) —
        # find the matching dict by `agent_key == match_key` and return the
        # `field_name` field (e.g. system_prompt).
        if extractor and isinstance(value, list):
            match_key, field_name = extractor
            for item in value:
                if isinstance(item, dict) and item.get("agent_key") == match_key:
                    return item.get(field_name)
            return None
        if isinstance(value, str):
            return value
        return None
    except Exception as e:
        logger.warning(f"[canonical_registry] failed to import prompt for '{agent_key}': {e}")
        return None


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_user_prompt(agent_key: str, prompt: str) -> dict:
    """Validate a user-supplied prompt against the agent's required placeholders.

    Returns:
      {
        "valid": bool,                 # False when any required placeholder missing
        "missing_placeholders": [...], # required placeholders not present
        "missing_soft": [...],         # soft markers not present (warn only)
        "warnings": [...],             # human-readable warning strings
      }
    """
    required = get_required_placeholders(agent_key)
    soft = get_soft_placeholders(agent_key)
    text = prompt or ""

    missing = [p for p in required if p not in text]
    missing_soft = [p for p in soft if p not in text]
    warnings: list[str] = []
    if missing_soft:
        warnings.append(
            f"Soft markers not found: {', '.join(missing_soft)}. "
            "Agent output may not parse correctly."
        )

    return {
        "valid": len(missing) == 0,
        "missing_placeholders": missing,
        "missing_soft": missing_soft,
        "warnings": warnings,
    }


def prompt_matches_canonical(agent_key: str, prompt: Optional[str]) -> bool:
    """SHA-compare a stored prompt against the code canonical.

    Used at backfill time + in `update_agent` to decide whether the
    `is_user_customized` flag should flip. Whitespace-insensitive on
    leading/trailing only — internal whitespace counts.
    """
    if prompt is None:
        return False
    canonical = get_canonical_prompt(agent_key)
    if canonical is None:
        return False
    return _sha(prompt.strip()) == _sha(canonical.strip())


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Runtime resolvers — these are the central entry points the agent code
# calls to get the right prompt / tools at request time.
# ---------------------------------------------------------------------------

def resolve_system_prompt(
    db,
    agent_key: str,
    *,
    canonical_builder=None,
) -> tuple[str, str]:
    """Resolve the BASE system prompt for this agent's runtime call.

    Returns ``(prompt_text, source)`` where source is one of
    ``"canonical" | "user_customized" | "user_customized_invalid_fallback"``.

    The caller (e.g. `_build_system_prompt` in enhanced_costing_agent) is
    responsible for taking this base text and applying request-time injections
    (org defaults, training context, mode-conditional sections, privacy/travel
    blocks). All of those injections operate on the resolved base — the user
    edits the BASE only, never the injected runtime sections.

    Validation: if the user's prompt is missing a required placeholder, log a
    WARNING and fall back to canonical for THIS call (fail-safe). The user's
    prompt stays in the DB; UI validation will show the error and prevent the
    next save until they fix it.

    Args:
      db: SQLAlchemy session.
      agent_key: The agent's key (alias-resolved internally).
      canonical_builder: Optional fallback callable returning the canonical
        prompt if the registry can't import it (e.g. for agents that build
        their prompt programmatically). Falls back to `get_canonical_prompt`.
    """
    key = _canonical_key(agent_key)
    canonical_text = (
        get_canonical_prompt(key)
        if canonical_builder is None
        else canonical_builder()
    )

    # If the agent doesn't support user prompts at all, always return canonical.
    if not supports_user_prompt(key):
        return (canonical_text or "", "canonical")

    # Look up CustomAgent — never crash a runtime call on a config error.
    try:
        from app.models.agent_builder import CustomAgent
        custom = (
            db.query(CustomAgent)
            .filter(CustomAgent.agent_key == agent_key)  # use ORIGINAL key, not alias
            .first()
        )
        # Fall back to alias if direct lookup misses (some agents have rows
        # under their canonical key but call sites use aliases).
        if custom is None and agent_key != key:
            custom = (
                db.query(CustomAgent)
                .filter(CustomAgent.agent_key == key)
                .first()
            )
    except Exception as e:
        logger.warning(
            f"[canonical_registry] CustomAgent lookup failed for '{agent_key}': {e}; "
            "falling back to canonical."
        )
        return (canonical_text or "", "canonical")

    if (
        custom is None
        or not custom.is_user_customized
        or not (custom.system_prompt or "").strip()
    ):
        return (canonical_text or "", "canonical")

    # User has customized — validate before using.
    validation = validate_user_prompt(key, custom.system_prompt)
    if not validation["valid"]:
        logger.warning(
            f"[canonical_registry] agent='{agent_key}' user prompt missing required "
            f"placeholders {validation['missing_placeholders']} — falling back to "
            "canonical for this call. Fix in Admin → Agent Builder."
        )
        return (canonical_text or "", "user_customized_invalid_fallback")

    if validation["warnings"]:
        logger.info(
            f"[canonical_registry] agent='{agent_key}' using user_customized prompt "
            f"with warnings: {validation['warnings']}"
        )

    return (custom.system_prompt, "user_customized")


def resolve_unknown_tool_keys(keys: list[str]) -> list[str]:
    """The subset of ``keys`` no registered tool implements.

    For the API layer to surface in Agent Builder. An unknown key used to be
    intersected away with nothing but a log line — the UI implied an
    assignment the runtime discarded.
    """
    from app.services.langchain.tools.tool_loader import get_available_tool_keys

    available = set(get_available_tool_keys())
    return [k for k in keys if k not in available]


def resolve_tool_keys(
    db,
    agent_key: str,
    default_keys: Optional[list[str]] = None,
) -> tuple[list[str], str]:
    """Resolve the tool_keys list for this agent's runtime call.

    Returns ``(tool_keys, source)`` where source is one of
    ``"default" | "user_customized" | "user_customized_filtered"``.

    ``default_keys`` is optional and normally omitted: the canonical registry's
    ``default_tools`` is the source of truth. Graphs passing their own literal
    list is how deep_analyzer ran four tools while the registry declared six.

    A user's explicit Agent Builder assignment wins whole. It is validated
    against the tool registry — an unknown key is excluded and reported via the
    ``user_customized_filtered`` source (and ``resolve_unknown_tool_keys`` for
    the UI) — but it is never intersected with the defaults: narrowing or
    widening an agent's belt is exactly what the assignment UI is for.
    """
    key = _canonical_key(agent_key)
    if default_keys is None:
        default_keys = list(CANONICAL_AGENTS.get(key, {}).get("default_tools") or [])
    if not supports_user_tools(key):
        return (list(default_keys), "default")

    try:
        from app.models.agent_builder import CustomAgent, AgentTool
        custom = (
            db.query(CustomAgent)
            .filter(CustomAgent.agent_key == agent_key)
            .first()
        )
        if custom is None and agent_key != key:
            custom = db.query(CustomAgent).filter(CustomAgent.agent_key == key).first()
    except Exception as e:
        logger.warning(
            f"[canonical_registry] CustomAgent lookup failed for '{agent_key}': {e}; "
            "using default tools."
        )
        return (list(default_keys), "default")

    if (
        custom is None
        or not custom.is_user_customized
        or not custom.tools
    ):
        return (list(default_keys), "default")

    # custom.tools is a list of {"tool_id": N, "config": {...}} dicts.
    # Resolve tool_ids back to tool_keys.
    user_tool_ids = [
        t.get("tool_id") for t in (custom.tools or []) if isinstance(t, dict) and t.get("tool_id")
    ]
    if not user_tool_ids:
        return (list(default_keys), "default")

    try:
        rows = (
            db.query(AgentTool)
            .filter(AgentTool.id.in_(user_tool_ids))
            .all()
        )
        user_keys = [r.tool_key for r in rows if getattr(r, "tool_key", None)]
    except Exception as e:
        logger.warning(
            f"[canonical_registry] AgentTool lookup failed for '{agent_key}': {e}; "
            "using default tools."
        )
        return (list(default_keys), "default")

    if not user_keys:
        return (list(default_keys), "default")

    # Validate against the tool registry only. The old code intersected with
    # default_keys here, which silently dropped any UI assignment outside the
    # graph's hardcoded list — the user saw the tool assigned, the runtime
    # never loaded it.
    unknown = resolve_unknown_tool_keys(user_keys)
    known = [k for k in user_keys if k not in unknown]
    if unknown:
        logger.warning(
            f"[canonical_registry] agent='{agent_key}' assignment includes keys no "
            f"tool implements: {unknown} — excluded. The rest are honoured as-is."
        )

    if not known:
        return (list(default_keys), "user_customized_filtered")

    return (known, "user_customized")
