"""
DRPL Backend - Agent Builder Service
Agent CRUD, versioning, lifecycle management, and system agent seeding.
"""

import importlib
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session
from textwrap import dedent

from app.models.agent_builder import CustomAgent, AgentVersion

logger = logging.getLogger(__name__)


# --- Default system agents (mirrors agent_config_service.DEFAULT_AGENTS) ---

SYSTEM_AGENTS = [
    {
        "agent_key": "classifier",
        "display_name": "Tender Classifier",
        "description": "Categorizes tenders into Mechanical, Electrical, Civil, IT/Software, Materials/Supplies, Consulting, Other",
        "category": "tender_analysis",
        "agent_type": "chain_of_thought",
        "temperature": 0.3,
        "max_tokens": 256,
    },
    {
        "agent_key": "relevance",
        "display_name": "Relevance Scorer",
        "description": "Scores tender relevance to DRPL Railway mechanical/electrical capabilities (0-1)",
        "category": "tender_analysis",
        "agent_type": "chain_of_thought",
        "temperature": 0.3,
        "max_tokens": 256,
    },
    {
        "agent_key": "risk",
        "display_name": "Risk Assessor",
        "description": "Assesses tender risk factors including deadlines, EMD, scope clarity (0-1)",
        "category": "tender_analysis",
        "agent_type": "chain_of_thought",
        "temperature": 0.3,
        "max_tokens": 256,
    },
    {
        "agent_key": "summary",
        "display_name": "Tender Summarizer",
        "description": "Generates 2-3 sentence executive summaries of tender scope and requirements",
        "category": "tender_analysis",
        "agent_type": "chain_of_thought",
        "temperature": 0.5,
        "max_tokens": 512,
    },
    {
        "agent_key": "eligibility",
        "display_name": "Eligibility Checker",
        "description": "AI-based eligibility assessment checking DRPL qualifications against tender requirements",
        "category": "tender_analysis",
        "agent_type": "chain_of_thought",
        "temperature": 0.4,
        "max_tokens": 512,
    },
    {
        "agent_key": "checklist",
        "display_name": "Checklist Generator",
        "description": "Extracts required documents and compliance items from tender specifications",
        "category": "tender_analysis",
        "agent_type": "chain_of_thought",
        "temperature": 0.3,
        "max_tokens": 2048,
    },
    {
        "agent_key": "proposal",
        "display_name": "Proposal Writer",
        "description": "Streaming chat agent specialized in Indian Railways tender proposal creation (IREPS, GeM, CPPP)",
        "category": "proposal",
        "agent_type": "chain_of_thought",
        "temperature": 0.7,
        "max_tokens": 4096,
    },
    {
        "agent_key": "document_analyzer",
        "display_name": "Document Analyzer",
        "description": "Deep extraction of requirements, eligibility, terms, critical clauses from tender documents with OCR support",
        "category": "document",
        "agent_type": "chain_of_thought",
        "temperature": 0.2,
        "max_tokens": 4096,
    },
    # --- LangChain-powered agents (react type = real tool calling) ---
    {
        "agent_key": "deep_analyzer",
        "display_name": "Deep Document Analyzer (LangChain)",
        "description": "LangChain ReAct agent that reads all tender documents, extracts requirements across 7 categories, detects negative keywords and rejection sentences, and identifies critical clauses. Uses document_reader, tender_lookup, and memory tools.",
        "category": "document",
        "agent_type": "react",
        "temperature": 0.2,
        "max_tokens": 8192,
    },
    {
        "agent_key": "proposal_creator",
        "display_name": "Proposal Creator (LangChain)",
        "description": "LangChain ReAct agent that generates tender proposal documents per checklist. Uses company profile data, AI document generation, letterhead/signatures, and web research. Produces text first, then applies formatting and PDF generation.",
        "category": "proposal",
        "agent_type": "react",
        "temperature": 0.7,
        "max_tokens": 8192,
    },
    {
        "agent_key": "costing_researcher",
        "display_name": "Costing Researcher (LangChain)",
        "description": "LangChain ReAct agent that researches market rates, applies DRPL costing rules (markup, overhead, GST), and produces detailed cost breakdowns. Uses web search, cost calculator, rate cards, and long-term memory.",
        "category": "proposal",
        "agent_type": "react",
        "temperature": 0.3,
        "max_tokens": 8192,
    },
    {
        "agent_key": "tender_pipeline",
        "display_name": "Tender Pipeline Orchestrator",
        "description": "Orchestrator agent that runs the full 4-step tender processing pipeline: Document Analysis → Checklist Generation → Document Generation → Costing Research. Coordinates all LangChain agents.",
        "category": "orchestration",
        "agent_type": "orchestrator",
        "temperature": 0.3,
        "max_tokens": 4096,
    },
    {
        "agent_key": "proposal_router",
        "display_name": "Proposal Chat Router",
        "description": "Intelligent orchestrator that classifies user intent from chat messages and routes to specialized agents (Deep Analyzer, Checklist Generator, Proposal Creator, Costing Researcher). Supports multi-agent chaining for complex requests.",
        "category": "orchestration",
        "agent_type": "orchestrator",
        "temperature": 0.2,
        "max_tokens": 1024,
    },
    {
        "agent_key": "checklist_generator",
        "display_name": "Checklist Generator (LangChain)",
        "description": "LangChain agent that generates submission checklists from tender documents. Extracts required documents, compliance items, and mandatory submissions with format requirements.",
        "category": "document",
        "agent_type": "react",
        "temperature": 0.3,
        "max_tokens": 4096,
    },
    {
        "agent_key": "workspace_manager",
        "display_name": "Workspace Manager",
        "description": "Creates and manages per-document workspaces for each checklist item. Initializes DocumentWorkspace rows, auto-matches format templates, and tracks review status. Read-only system agent.",
        "category": "document",
        "agent_type": "chain_of_thought",
        "temperature": 0.2,
        "max_tokens": 1024,
    },
    {
        "agent_key": "annexure_finder",
        "display_name": "Annexure Finder",
        "description": "Claude Vision agent that identifies every annexure, schedule, proforma, declaration, and certificate in a tender's PDFs and materializes each as an editable workspace document with [Fill: …] placeholders. Preserves original structure (headings, tables, signature blocks) for faithful reproduction.",
        "category": "document",
        "agent_type": "chain_of_thought",
        "temperature": 0.2,
        "max_tokens": 16384,
        "_system_prompt_module": "app.services.langchain.graphs.annexure_finder_agent",
        # Pass 1 (discovery) is what this row configures; the registry has
        # declared ANNEXURE_DISCOVERY_PROMPT its canonical since the two-pass
        # split. Seeding the legacy combined EXTRACTION prompt made the row
        # differ from its canonical, get flagged as an admin edit, and run
        # the body-emitting prompt in pass 1 (~14k output tokens per PDF).
        "_system_prompt_attr": "ANNEXURE_DISCOVERY_PROMPT",
    },
    {
        "agent_key": "decision_maker",
        "display_name": "Decision Maker (Master Agent)",
        "description": "Autonomous master orchestrator above the specialized agent catalog. Plans multi-step workflows, calls any specialized agent (deep_analyzer, checklist_generator, proposal_creator, costing_researcher, workspace_manager, annexure_finder) as a sub-tool, inspects platform state, repairs broken workspaces/checklists, and picks the best LLM provider (Anthropic / OpenAI / Google) per subtask. Triggers automatically on multi-step, cross-agent, conditional, or troubleshooting requests and streams its full Thought / Action / Observation timeline inline in the chat.",
        "category": "orchestration",
        "agent_type": "react",
        "temperature": 0.2,
        "max_tokens": 8192,
    },
    # The self-hosted model's only registered use, and deliberately the most
    # boring one on the platform: it answers a fixed question about itself.
    #
    # It exists so the local endpoint is a real, monitored agent -- it appears
    # in Agent Builder like every other one, an admin can Run it from there,
    # and its calls land in `api_usage_logs` under provider `runpod` so the AI
    # usage dashboard counts them -- while sitting on ZERO production paths.
    # Nothing routes to it, nothing delegates to it, no pipeline calls it, and
    # `PREFERRED_AGENT_MODELS` does not name it, so the startup refresh leaves
    # its provider and model alone. If the endpoint is cold, misconfigured or
    # gone, the only thing that fails is a button an admin pressed.
    #
    # Qwen2.5-7B-Instruct is text-only. That is the standing reason this agent
    # is not a stepping stone to the annexure or analyzer paths, whatever its
    # cost looks like: those read pages, and this model cannot see one.
    {
        "agent_key": "local_model_probe",
        "display_name": "Local Model Probe (RunPod Qwen)",
        "description": (
            "Health and capability probe for the self-hosted model on the "
            "RunPod serverless vLLM endpoint (Qwen2.5-7B-Instruct, "
            "OpenAI-compatible). Runs on no production path: it exists so the "
            "local endpoint is reachable, attributable in the usage log and "
            "visible here before any real work is moved onto it. Text-only, "
            "so it can never be used for anything that reads a PDF page."
        ),
        "category": "general",
        "agent_type": "chain_of_thought",
        "provider": "runpod",
        "model": "qwen2.5-7b",
        # Deterministic: a probe that answers differently each time cannot
        # tell you whether the endpoint changed.
        "temperature": 0.0,
        "max_tokens": 512,
        "system_prompt": dedent(
            # Triple-quoted on purpose: the prompt has real line breaks
            # rather than escapes, so it reads here as the model sees it.
            """
            You are a self-hosted model running on DRPL's own GPU endpoint, used only
            as a reachability and capability probe.

            Answer in at most three short lines:
            1. The model you are, exactly as you are served.
            2. Whether you can read images or PDF pages (you cannot).
            3. One sentence on what you are suitable for.

            Never guess about the platform, its tenders, its costings or its data. You
            have no access to any of it and no tools. If you are asked anything beyond
            the three lines above, say that this agent is a probe with no access to
            platform data.
            """
        ).strip(),
    },
]


# --- Version helpers ---

def _create_version_snapshot(db: Session, agent: CustomAgent, user_id: Optional[int] = None,
                             change_description: Optional[str] = None) -> AgentVersion:
    """Create an AgentVersion snapshot of the current agent configuration."""
    version = AgentVersion(
        agent_id=agent.id,
        version_number=agent.current_version,
        system_prompt=agent.system_prompt,
        tools=agent.tools,
        temperature=agent.temperature,
        max_tokens=agent.max_tokens,
        model=agent.model,
        orchestration_config=agent.orchestration_config,
        change_description=change_description,
        created_by=user_id,
    )
    db.add(version)
    return version


# --- CRUD ---

_AGENTS_CACHE_PREFIX = "drpl:cache:custom_agents"


def _invalidate_agents_cache() -> None:
    """Drop every cached variant of list_agents (category/published filters)."""
    try:
        from app.core.redis_client import cache_delete_prefix
        cache_delete_prefix(_AGENTS_CACHE_PREFIX)
    except Exception as e:
        logger.debug(f"agents cache invalidate: {e}")


def create_agent(db: Session, data: dict, user_id: Optional[int] = None) -> CustomAgent:
    """
    Create a new CustomAgent and its initial version snapshot.

    Args:
        db: Database session.
        data: Dict with agent fields (agent_key, display_name, system_prompt, etc.).
        user_id: ID of the creating user.

    Returns:
        The newly created CustomAgent.
    """
    agent = CustomAgent(
        agent_key=data["agent_key"],
        display_name=data["display_name"],
        description=data.get("description"),
        agent_type=data.get("agent_type", "chain_of_thought"),
        system_prompt=data.get("system_prompt"),
        model=data.get("model"),
        provider=data.get("provider", "anthropic"),
        temperature=data.get("temperature", 0.7),
        max_tokens=data.get("max_tokens", 4096),
        tools=data.get("tools", []),
        input_schema=data.get("input_schema"),
        output_schema=data.get("output_schema"),
        orchestration_config=data.get("orchestration_config"),
        langchain_config=data.get("langchain_config"),
        mcp_servers=data.get("mcp_servers", []),
        learning_enabled=data.get("learning_enabled", True),
        thinking_mode=data.get("thinking_mode"),
        thinking_budget_tokens=data.get("thinking_budget_tokens"),
        effort=data.get("effort"),
        is_system=data.get("is_system", False),
        is_enabled=data.get("is_enabled", True),
        is_published=data.get("is_published", False),
        category=data.get("category"),
        tags=data.get("tags", []),
        current_version=1,
        created_by=user_id,
    )
    db.add(agent)
    db.flush()  # Populate agent.id for the version FK

    _create_version_snapshot(db, agent, user_id=user_id, change_description="Initial version")
    db.commit()
    db.refresh(agent)
    _invalidate_agents_cache()
    logger.info(f"Created agent '{agent.agent_key}' (id={agent.id})")
    return agent


def update_agent(db: Session, agent_id: int, data: dict, user_id: Optional[int] = None) -> Optional[CustomAgent]:
    """
    Update an existing agent and auto-create a new version snapshot.

    Args:
        db: Database session.
        agent_id: ID of the agent to update.
        data: Dict of fields to update.
        user_id: ID of the updating user.

    Returns:
        The updated CustomAgent, or None if not found.
    """
    agent = db.query(CustomAgent).filter(CustomAgent.id == agent_id).first()
    if not agent:
        return None

    change_description = data.pop("change_description", None)

    # Phase 3d — capture pre-update state of customization-relevant fields so
    # we can flip is_user_customized correctly on prompt/tools changes.
    pre_prompt = agent.system_prompt
    pre_tools = agent.tools

    for key, value in data.items():
        if hasattr(agent, key) and key not in ("id", "agent_key", "created_by", "created_at"):
            setattr(agent, key, value)

    # Phase 3d — auto-flip is_user_customized when prompt/tools actually changed.
    # Compare against canonical (not just pre-update value) so a "no-op edit"
    # that pastes the canonical back doesn't keep the flag set.
    try:
        from app.services.langchain.canonical_registry import (
            prompt_matches_canonical,
            get_canonical_tools,
            is_registered as _is_in_registry,
        )
        if _is_in_registry(agent.agent_key):
            prompt_changed = "system_prompt" in data and data.get("system_prompt") != pre_prompt
            tools_changed = "tools" in data and data.get("tools") != pre_tools
            if prompt_changed or tools_changed:
                # If the new state matches canonical exactly, treat as not-customized
                # (e.g. user pasted the canonical back to "undo" a prior edit).
                prompt_matches = prompt_matches_canonical(
                    agent.agent_key, agent.system_prompt
                )
                canonical_tools = get_canonical_tools(agent.agent_key)
                # Tool comparison: compare resolved tool_keys of both sides.
                current_tool_keys = _tool_objs_to_keys(db, agent.tools or [])
                tools_match = (
                    sorted(current_tool_keys) == sorted(canonical_tools)
                    or not current_tool_keys  # empty tools = use canonical
                )
                agent.is_user_customized = not (prompt_matches and tools_match)
                logger.info(
                    f"[update_agent] '{agent.agent_key}' prompt_changed={prompt_changed} "
                    f"tools_changed={tools_changed} → is_user_customized={agent.is_user_customized}"
                )
    except Exception as e:
        logger.debug(f"[update_agent] is_user_customized auto-flip failed (non-fatal): {e}")

    agent.current_version += 1
    agent.updated_at = datetime.now(timezone.utc)

    _create_version_snapshot(db, agent, user_id=user_id, change_description=change_description)
    db.commit()
    db.refresh(agent)
    _invalidate_agents_cache()
    logger.info(
        f"Updated agent '{agent.agent_key}' to version {agent.current_version} "
        f"(is_user_customized={agent.is_user_customized})"
    )
    return agent


def _tool_objs_to_keys(db: Session, tool_objs: list) -> list[str]:
    """Resolve a CustomAgent.tools list-of-dicts to a list of tool_keys."""
    if not tool_objs:
        return []
    try:
        ids = [
            t.get("tool_id") for t in tool_objs
            if isinstance(t, dict) and t.get("tool_id")
        ]
        if not ids:
            return []
        rows = db.query(AgentTool).filter(AgentTool.id.in_(ids)).all()
        return [r.tool_key for r in rows if getattr(r, "tool_key", None)]
    except Exception:
        return []


def delete_agent(db: Session, agent_id: int) -> bool:
    """
    Delete an agent and all related versions and test cases.

    Returns:
        True if the agent was deleted, False if not found.
    """
    agent = db.query(CustomAgent).filter(CustomAgent.id == agent_id).first()
    if not agent:
        return False

    # Remove related versions
    db.query(AgentVersion).filter(AgentVersion.agent_id == agent_id).delete()
    db.delete(agent)
    db.commit()
    _invalidate_agents_cache()
    logger.info(f"Deleted agent '{agent.agent_key}' (id={agent_id})")
    return True


def list_agents(db: Session, category: Optional[str] = None,
                is_published: Optional[bool] = None) -> list[CustomAgent]:
    """List agents with optional filters."""
    query = db.query(CustomAgent)
    if category is not None:
        query = query.filter(CustomAgent.category == category)
    if is_published is not None:
        query = query.filter(CustomAgent.is_published == is_published)
    return query.order_by(CustomAgent.display_name).all()


def get_agent(db: Session, agent_id: int) -> Optional[CustomAgent]:
    """Get a single agent by ID."""
    return db.query(CustomAgent).filter(CustomAgent.id == agent_id).first()


def get_agent_by_key(db: Session, agent_key: str) -> Optional[CustomAgent]:
    """Lookup an agent by its unique key slug."""
    return db.query(CustomAgent).filter(CustomAgent.agent_key == agent_key).first()


def get_agent_versions(db: Session, agent_id: int) -> list[AgentVersion]:
    """Get the full version history for an agent, newest first."""
    return (
        db.query(AgentVersion)
        .filter(AgentVersion.agent_id == agent_id)
        .order_by(AgentVersion.version_number.desc())
        .all()
    )


def rollback_agent(db: Session, agent_id: int, version_number: int,
                   user_id: Optional[int] = None) -> Optional[CustomAgent]:
    """
    Restore an agent to a previous version.

    This creates a *new* version (current_version + 1) whose contents match
    the requested historical version, preserving the full audit trail.
    """
    agent = db.query(CustomAgent).filter(CustomAgent.id == agent_id).first()
    if not agent:
        return None

    target_version = (
        db.query(AgentVersion)
        .filter(AgentVersion.agent_id == agent_id, AgentVersion.version_number == version_number)
        .first()
    )
    if not target_version:
        return None

    # Apply snapshot fields back to the agent
    agent.system_prompt = target_version.system_prompt
    agent.tools = target_version.tools
    agent.temperature = target_version.temperature
    agent.max_tokens = target_version.max_tokens
    agent.model = target_version.model
    agent.orchestration_config = target_version.orchestration_config
    agent.current_version += 1
    agent.updated_at = datetime.now(timezone.utc)

    _create_version_snapshot(
        db, agent, user_id=user_id,
        change_description=f"Rolled back to version {version_number}",
    )
    db.commit()
    db.refresh(agent)
    logger.info(f"Rolled back agent '{agent.agent_key}' to version {version_number} (new version {agent.current_version})")
    return agent


def publish_agent(db: Session, agent_id: int) -> Optional[CustomAgent]:
    """Set an agent as published (visible in the library)."""
    agent = db.query(CustomAgent).filter(CustomAgent.id == agent_id).first()
    if not agent:
        return None
    agent.is_published = True
    agent.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(agent)
    return agent


def clone_agent(db: Session, agent_id: int, new_name: str,
                user_id: Optional[int] = None) -> Optional[CustomAgent]:
    """
    Duplicate an existing agent under a new name/key.

    Returns:
        The newly created clone, or None if the source agent was not found.
    """
    source = db.query(CustomAgent).filter(CustomAgent.id == agent_id).first()
    if not source:
        return None

    # Derive a unique key from the new name
    new_key = new_name.lower().replace(" ", "_")

    clone_data = {
        "agent_key": new_key,
        "display_name": new_name,
        "description": source.description,
        "agent_type": source.agent_type,
        "system_prompt": source.system_prompt,
        "model": source.model,
        "provider": source.provider,
        "temperature": source.temperature,
        "max_tokens": source.max_tokens,
        "tools": source.tools,
        "input_schema": source.input_schema,
        "output_schema": source.output_schema,
        "orchestration_config": source.orchestration_config,
        "category": source.category,
        "tags": source.tags,
        "is_system": False,
        "is_published": False,
    }
    return create_agent(db, clone_data, user_id=user_id)


def _resolve_system_prompt(defaults: dict) -> Optional[str]:
    """Lazily import a system prompt from another module if _system_prompt_module is set."""
    mod_path = defaults.get("_system_prompt_module")
    attr_name = defaults.get("_system_prompt_attr")
    if not mod_path or not attr_name:
        return defaults.get("system_prompt")
    try:
        import importlib
        mod = importlib.import_module(mod_path)
        return getattr(mod, attr_name, None)
    except Exception as e:
        logger.warning(f"Could not resolve system prompt from {mod_path}.{attr_name}: {e}")
        return None


#: Prompts a previous version of SYSTEM_AGENTS seeded, per agent. A stored
#: prompt equal to one of these was written by the seeder, not an admin, so
#: it is re-synced instead of being preserved as a customisation.
_SUPERSEDED_SEED_PROMPTS: dict[str, tuple[tuple[str, str], ...]] = {
    "annexure_finder": (
        ("app.services.langchain.graphs.annexure_finder_agent", "ANNEXURE_EXTRACTION_PROMPT"),
    ),
}


def _is_superseded_seed_prompt(agent_key: str, prompt: Optional[str]) -> bool:
    if not prompt:
        return False
    for module_path, attr in _SUPERSEDED_SEED_PROMPTS.get(agent_key, ()):
        try:
            module = importlib.import_module(module_path)
            if (getattr(module, attr, None) or "").strip() == prompt.strip():
                return True
        except Exception:
            continue
    return False


def seed_system_agents(db: Session) -> int:
    """
    Seed CustomAgent records for the built-in system agents.

    Creates agents whose agent_key doesn't exist yet. For existing agents:
      - When NOT user-customized: re-sync `system_prompt` from the code
        canonical so improvements to the canonical prompt flow through.
      - When user-customized (`is_user_customized=True`): skip prompt re-sync
        so admin edits made in the UI survive backend restarts.

    First-startup SHA backfill: if the existing prompt SHA-matches the code
    canonical, mark `is_user_customized=False`. If it differs, treat as
    user-customized to preserve any prior edits — admin can still hit
    "Reset to Default" via the UI to restore canonical.

    Returns:
        Number of newly created agents.
    """
    from app.services.langchain.canonical_registry import (
        prompt_matches_canonical,
        is_registered as _is_in_registry,
    )

    created = 0
    for defaults in SYSTEM_AGENTS:
        # Strip internal meta-keys before passing to create/update
        clean = {k: v for k, v in defaults.items() if not k.startswith("_")}
        resolved_prompt = _resolve_system_prompt(defaults)
        if resolved_prompt:
            clean["system_prompt"] = resolved_prompt

        existing = db.query(CustomAgent).filter(CustomAgent.agent_key == clean["agent_key"]).first()
        if existing and _is_superseded_seed_prompt(existing.agent_key, existing.system_prompt):
            # Written by an older seed, not by an admin: clear the false
            # customisation so the re-sync below restores the canonical.
            existing.is_user_customized = False
            existing.system_prompt = resolved_prompt or existing.system_prompt
            existing.updated_at = datetime.now(timezone.utc)
            logger.info(
                f"[seed] '{existing.agent_key}' held a superseded seed prompt -- "
                f"restored the canonical and cleared the customisation flag"
            )
        if existing:
            # First-startup backfill of is_user_customized — if the stored
            # prompt SHA-matches the canonical, treat as not-customized so
            # the seed re-sync below applies. (Idempotent: subsequent
            # restarts hit the same branch unless an admin actually edits.)
            if _is_in_registry(existing.agent_key):
                if prompt_matches_canonical(existing.agent_key, existing.system_prompt):
                    if existing.is_user_customized:
                        existing.is_user_customized = False
                        logger.info(
                            f"[seed] '{existing.agent_key}' SHA-matches canonical — "
                            f"resetting is_user_customized to False"
                        )
                else:
                    # Stored prompt differs from canonical AND there IS a
                    # canonical we could reset to. Only flip the flag on the
                    # FIRST observation (avoid log spam on every restart).
                    if not existing.is_user_customized and existing.system_prompt:
                        existing.is_user_customized = True
                        logger.info(
                            f"[seed] '{existing.agent_key}' has user edits "
                            f"(prompt differs from canonical) — flagging is_user_customized=True"
                        )

            if existing.is_user_customized:
                logger.info(
                    f"[seed] '{existing.agent_key}' is user-customized — "
                    f"skipping prompt+tools resync to preserve admin edits"
                )
            else:
                # Compare before writing: this runs on every boot and every
                # Agent Builder list load, and an unconditional write bumped
                # updated_at on ~20 rows each time.
                changed = False
                if resolved_prompt and existing.system_prompt != resolved_prompt:
                    existing.system_prompt = resolved_prompt
                    changed = True
                # Tools: only re-sync when the seed defaults provide them
                # (most system agents leave tools empty since they're
                # programmatic).
                if clean.get("tools") and existing.tools != clean["tools"]:
                    existing.tools = clean["tools"]
                    changed = True
                if changed:
                    existing.updated_at = datetime.now(timezone.utc)
                    logger.info(f"[seed] '{existing.agent_key}' re-synced from code canonical")

            db.commit()
            continue

        agent_data = {
            **clean,
            "is_system": True,
            "is_enabled": True,
            "is_published": True,
        }
        create_agent(db, agent_data, user_id=None)
        created += 1

    logger.info(f"Seeded {created} system agents ({len(SYSTEM_AGENTS) - created} already existed)")
    return created
