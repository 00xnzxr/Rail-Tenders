"""
DRPL Backend - Agent Builder Routes
Visual agent builder, execution, testing, tools, and library endpoints
"""

from typing import Optional, List
from fastapi import APIRouter, Depends, Query, HTTPException, UploadFile, File, Form
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.auth import get_current_user, require_master_admin
from app.models.user import User
from app.models.agent_builder import CustomAgent, AgentTool
from app.services.agent_builder_service import (
    create_agent, update_agent, delete_agent, list_agents, get_agent,
    get_agent_versions, rollback_agent, publish_agent, clone_agent,
    seed_system_agents,
)
from app.services.agent_execution_service import (
    execute_agent, get_execution, get_agent_executions, get_execution_stats,
)
from app.services.agent_testing_service import (
    create_test_case, update_test_case, delete_test_case, get_test_cases,
    run_test_case, run_all_tests,
)
from app.services.agent_tools_service import (
    create_tool, update_tool, list_tools, get_tool, delete_tool,
)
from app.services.test_document_service import (
    upload_test_document, list_test_documents, get_test_document,
    delete_test_document, delete_all_test_documents, re_extract_text,
    build_documents_context,
)

router = APIRouter(prefix="/agent-builder", tags=["agent-builder"])


# --- Agent CRUD ---

#: The Master Agent governs the platform and calls every other agent. It is
#: presented separately in the admin UI, and it is never a worker: it must not
#: appear in its own callable roster (that is unbounded recursion with a
#: per-call price tag).
MASTER_AGENT_KEY = "decision_maker"

#: Agents that exist to serve the routing layer rather than to do tender work.
_INFRASTRUCTURE_AGENT_KEYS = {"proposal_router"}


def _agent_role(agent_key: str) -> str:
    """"master" | "infrastructure" | "worker"."""
    if agent_key == MASTER_AGENT_KEY:
        return "master"
    if agent_key in _INFRASTRUCTURE_AGENT_KEYS:
        return "infrastructure"
    return "worker"


#: Agents whose chat wrapper loads a fixed tool set at runtime, ignoring the
#: `tools` column. Kept here so the UI can show what the agent actually runs
#: with — the Tools tab used to show the stored list, which for these agents
#: has no relationship to reality.
_WRAPPER_TOOL_KEYS: dict[str, list[str]] = {
    "proposal_creator": [
        "document_generator", "web_search", "memory_retrieve", "memory_store",
        "clarify",
    ],
    "workspace_manager": [
        "workspace_init", "workspace_status", "workspace_list_items",
        "workspace_generate_document", "tender_lookup", "clarify",
    ],
}


@router.get("/agents/{agent_id}/effective-tools")
def get_effective_tools(
    agent_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_master_admin),
):
    """What this agent will ACTUALLY run with, and where that comes from.

    The `tools` column is only one of four sources. The Master Agent builds its
    catalog at runtime and stores nothing; two chat wrappers hardcode a fixed
    set; canonical agents fall back to registry defaults when the column is
    empty. Showing only the stored list made the Tools tab misleading — the
    Master Agent displayed zero tools while running with more than thirty.
    """
    from app.services.langchain.tool_policy import classify_tool

    agent = db.query(CustomAgent).filter(CustomAgent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    key = agent.agent_key
    stored_count = len(agent.tools or [])

    def described(names: list[str], source: str, note: str) -> dict:
        return {
            "source": source,
            "note": note,
            "stored_tool_count": stored_count,
            "tools": [
                {"name": n, "tier": classify_tool(n)} for n in sorted(set(names))
            ],
        }

    # 1. The Master Agent's catalog is built per run from the live registry.
    if key == MASTER_AGENT_KEY:
        from app.services.langchain.graphs.orchestrator_tools import (
            build_action_tools, build_agent_wrapper_tools,
            build_diagnostic_tools, build_llm_meta_tools,
        )
        from app.services.langchain.graphs.platform_tools import build_platform_tools
        from app.services.langchain.graphs.quality_tools import build_quality_tools

        names = [
            *(t.name for t in build_agent_wrapper_tools(None, None, current_user.id, db=db)),
            *(t.name for t in build_diagnostic_tools(user_id=current_user.id)),
            *(t.name for t in build_action_tools(user_id=current_user.id)),
            *(t.name for t in build_llm_meta_tools()),
            *(t.name for t in build_platform_tools(db, current_user.id, current_user.role)),
            *(t.name for t in build_quality_tools(db, current_user.id)),
            "ask_user",
        ]
        return described(
            names,
            "runtime",
            "Built fresh on every run from the agent registry and this user's "
            "role. Not editable here — enable or disable worker agents to "
            "change what it can call.",
        )

    # 2. Chat wrappers with a fixed set.
    if key in _WRAPPER_TOOL_KEYS:
        return described(
            _WRAPPER_TOOL_KEYS[key],
            "wrapper",
            "This agent runs through a purpose-built handler that loads a fixed "
            "tool set. Tools configured here are not used on that path.",
        )

    # 4. An empty assignment means the shared repo, not nothing.
    from app.services.langchain.tools.tool_loader import resolve_agent_tool_keys

    shared = resolve_agent_tool_keys(key, agent.tools or [])
    if shared is not None:
        if shared:
            return described(
                shared,
                "shared-repo",
                "No tools are configured, so this agent reaches the whole shared "
                "tool repository. Assign tools below only if you want to narrow "
                "it to a specific set.",
            )
        return described(
            [],
            "tool-free",
            "This agent makes one short structured call and is deliberately "
            "kept tool-free — it runs at high volume, where the tool "
            "definitions would cost more than they are worth.",
        )

    # 5. An explicit, narrowed assignment.
    names = []
    for entry in agent.tools or []:
        tool_id = entry.get("tool_id") if isinstance(entry, dict) else entry
        row = db.query(AgentTool).filter(AgentTool.id == tool_id).first()
        if row:
            names.append(row.tool_key)
    return described(
        names,
        "configured",
        "This agent is narrowed to these tools. Clear them to give it the "
        "whole shared repository instead.",
    )


@router.get("/models")
def list_models(current_user: User = Depends(require_master_admin)):
    """The model catalog the Agent Builder renders from.

    This exists because the frontend used to carry its own hardcoded copies of
    the model list, the provider list and the per-model output ceilings. They
    drifted, and the drift was invisible: an agent saved as claude-opus-5 was
    displayed as claude-sonnet-4-6 simply because the new ID was not in the
    frontend's array, and the max-tokens hint showed a fallback rather than the
    real ceiling. One list, served from the same tables the runtime uses.
    """
    from app.services.ai_service import (
        ADAPTIVE_THINKING_MODELS,
        EFFORT_SUPPORTED_MODELS,
        EXTENDED_THINKING_MODELS,
        MAX_EFFORT_MODELS,
        VALID_EFFORT_LEVELS,
        supports_sampling_params,
    )
    from app.services.langchain.model_limits import (
        DEFAULT_MAX_OUTPUT,
        MODEL_MAX_OUTPUT_TOKENS,
    )
    from app.services.langchain.provider_config import PRICING
    from app.services.seed_agent_models import TIER_MODELS

    def provider_of(model_id: str) -> str:
        if model_id.startswith("claude-"):
            return "anthropic"
        if model_id.startswith(("gpt-", "o1", "o3", "o4")):
            return "openai"
        if model_id.startswith("gemini-"):
            return "google"
        if model_id.lower().startswith("qwen") or "/" in model_id:
            # Self-hosted: the name vLLM serves the weights as, or a bare
            # Hugging Face repo id.
            return "runpod"
        return "other"

    # Current = whatever the tier map points at. Everything else is offered but
    # marked superseded, so an agent pinned to an old model still displays its
    # real value instead of silently rendering as something else.
    current_ids = {m for tiers in TIER_MODELS.values() for m in tiers.values()}
    tier_of = {
        model: tier
        for tiers in TIER_MODELS.values()
        for tier, model in tiers.items()
    }

    models = []
    for model_id in sorted(PRICING):
        price = PRICING[model_id]
        models.append({
            "id": model_id,
            "provider": provider_of(model_id),
            "tier": tier_of.get(model_id),
            "is_current": model_id in current_ids,
            "max_output_tokens": MODEL_MAX_OUTPUT_TOKENS.get(model_id, DEFAULT_MAX_OUTPUT),
            "input_price_per_mtok": price["input"],
            "output_price_per_mtok": price["output"],
            "supports_adaptive_thinking": model_id in ADAPTIVE_THINKING_MODELS,
            "supports_thinking_budget": model_id in EXTENDED_THINKING_MODELS,
            "supports_effort": model_id in EFFORT_SUPPORTED_MODELS,
            "supports_max_effort": model_id in MAX_EFFORT_MODELS,
            "supports_sampling": supports_sampling_params(model_id),
        })

    return {
        "models": models,
        "providers": sorted({m["provider"] for m in models} - {"other"}),
        "effort_levels": sorted(VALID_EFFORT_LEVELS),
        "default_max_output_tokens": DEFAULT_MAX_OUTPUT,
    }


@router.get("/agents")
def api_list_agents(
    category: Optional[str] = Query(None),
    is_published: Optional[bool] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_master_admin),
):
    """List all custom agents. Cached in Redis (invalidated on agent CRUD)."""
    seed_system_agents(db)
    from app.core.redis_client import cache_get_json, cache_set_json, cache_key
    pub_part = "any" if is_published is None else ("pub" if is_published else "draft")
    # v2: the payload gained `agent_role`. Without bumping this, a cached v1
    # entry keeps serving rows with no role until its TTL expires, and the UI
    # would group every agent as a worker — including the Master Agent.
    ck = cache_key("custom_agents_v2", category or "all", pub_part)
    cached = cache_get_json(ck)
    if cached is not None:
        return cached

    agents = list_agents(db, category=category, is_published=is_published)
    result = [
        {
            "id": a.id, "agent_key": a.agent_key, "display_name": a.display_name,
            "description": a.description, "agent_type": a.agent_type,
            "model": a.model, "provider": a.provider,
            "temperature": a.temperature, "max_tokens": a.max_tokens,
            "is_system": a.is_system, "is_enabled": a.is_enabled,
            "is_published": a.is_published, "category": a.category,
            "tags": a.tags, "current_version": a.current_version,
            "tools_count": len(a.tools or []),
            # Role in the hierarchy, so the UI can present the Master Agent
            # above the workers it directs rather than mixed in with them.
            "agent_role": _agent_role(a.agent_key),
            "created_at": a.created_at, "updated_at": a.updated_at,
        }
        for a in agents
    ]
    cache_set_json(ck, result)
    return result


@router.post("/agents")
def api_create_agent(
    body: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_master_admin),
):
    """Create a new custom agent."""
    agent = create_agent(db, body, current_user.id)
    return {"id": agent.id, "agent_key": agent.agent_key, "status": "created"}


@router.get("/agents/{agent_id}")
def api_get_agent(
    agent_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_master_admin),
):
    """Get agent details."""
    agent = get_agent(db, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    return {
        "id": agent.id, "agent_key": agent.agent_key, "display_name": agent.display_name,
        "description": agent.description, "agent_type": agent.agent_type,
        "system_prompt": agent.system_prompt, "model": agent.model,
        "provider": agent.provider, "temperature": agent.temperature,
        "max_tokens": agent.max_tokens, "tools": agent.tools,
        "input_schema": agent.input_schema, "output_schema": agent.output_schema,
        "orchestration_config": agent.orchestration_config,
        "langchain_config": agent.langchain_config,
        "mcp_servers": agent.mcp_servers or [],
        "learning_enabled": agent.learning_enabled if agent.learning_enabled is not None else True,
        "thinking_mode": agent.thinking_mode,
        "thinking_budget_tokens": agent.thinking_budget_tokens,
        "effort": agent.effort,
        "is_system": agent.is_system, "is_enabled": agent.is_enabled,
        "is_published": agent.is_published, "category": agent.category,
        "is_user_customized": getattr(agent, "is_user_customized", False),
        "tags": agent.tags, "current_version": agent.current_version,
        "created_by": agent.created_by,
        "created_at": agent.created_at, "updated_at": agent.updated_at,
    }


@router.put("/agents/{agent_id}")
def api_update_agent(
    agent_id: int,
    body: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_master_admin),
):
    """Update agent (creates new version).

    Phase 3d — when `system_prompt` is changed, validate against the canonical
    registry's required placeholders. Hard failure (400) on missing required
    placeholders; soft warnings (e.g. missing structured-output markers) are
    surfaced in the response without blocking the save.
    """
    # Phase 3d — pre-validate prompt against the canonical registry.
    if "system_prompt" in body and body.get("system_prompt"):
        existing = db.query(CustomAgent).filter(CustomAgent.id == agent_id).first()
        if existing:
            try:
                from app.services.langchain.canonical_registry import (
                    is_registered as _is_in_registry,
                    supports_user_prompt as _supports_user_prompt,
                    validate_user_prompt,
                )
                if _is_in_registry(existing.agent_key):
                    if not _supports_user_prompt(existing.agent_key):
                        raise HTTPException(
                            status_code=400,
                            detail={
                                "message": (
                                    f"Agent '{existing.agent_key}' is system-managed and "
                                    "its system_prompt cannot be edited via the UI."
                                ),
                                "agent_key": existing.agent_key,
                            },
                        )
                    validation = validate_user_prompt(
                        existing.agent_key, body["system_prompt"]
                    )
                    if not validation["valid"]:
                        raise HTTPException(
                            status_code=400,
                            detail={
                                "message": (
                                    "system_prompt is missing required placeholders. "
                                    "These tokens are substituted at runtime with "
                                    "platform settings — removing them would silently "
                                    "break the agent."
                                ),
                                "validation": validation,
                            },
                        )
            except HTTPException:
                raise
            except Exception:
                # If validation infrastructure errors, fall through and let
                # the standard update proceed — don't block on a tool bug.
                pass

    agent = update_agent(db, agent_id, body, current_user.id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    response = {
        "id": agent.id, "current_version": agent.current_version,
        "is_user_customized": getattr(agent, "is_user_customized", False),
        "status": "updated",
    }
    # Surface soft-validation warnings in the response (e.g. missing markers).
    if "system_prompt" in body and body.get("system_prompt"):
        try:
            from app.services.langchain.canonical_registry import (
                is_registered as _is_in_registry,
                validate_user_prompt,
            )
            if _is_in_registry(agent.agent_key):
                v = validate_user_prompt(agent.agent_key, body["system_prompt"])
                if v.get("warnings"):
                    response["warnings"] = v["warnings"]
        except Exception:
            pass
    return response


# ---------------------------------------------------------------------------
# Phase 3d — canonical / reset-to-default / validate-prompt endpoints
# ---------------------------------------------------------------------------

@router.get("/agents/{agent_id}/canonical")
def api_get_canonical(
    agent_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_master_admin),
):
    """Return the code-canonical prompt + tools for this agent + a diff
    against the current stored version. Powers the frontend's "Customized"
    badge, "Reset to Default", and "View Diff" UX.
    """
    from app.services.langchain.canonical_registry import (
        is_registered as _is_in_registry,
        get_canonical_prompt,
        get_canonical_tools,
        get_required_placeholders,
        get_soft_placeholders,
        supports_user_prompt as _supports_user_prompt,
        supports_user_tools as _supports_user_tools,
        prompt_matches_canonical,
    )
    agent = db.query(CustomAgent).filter(CustomAgent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if not _is_in_registry(agent.agent_key):
        return {
            "agent_key": agent.agent_key,
            "registered": False,
            "message": (
                "This agent is not in the canonical registry. Reset-to-default + "
                "validation are only available for system agents."
            ),
        }
    canonical_prompt = get_canonical_prompt(agent.agent_key)
    canonical_tools = get_canonical_tools(agent.agent_key)
    return {
        "agent_key": agent.agent_key,
        "registered": True,
        "canonical_prompt": canonical_prompt,
        "canonical_tools": canonical_tools,
        "required_placeholders": get_required_placeholders(agent.agent_key),
        "soft_placeholders": get_soft_placeholders(agent.agent_key),
        "supports_user_prompt": _supports_user_prompt(agent.agent_key),
        "supports_user_tools": _supports_user_tools(agent.agent_key),
        "current_is_user_customized": getattr(agent, "is_user_customized", False),
        "current_prompt_matches_canonical": prompt_matches_canonical(
            agent.agent_key, agent.system_prompt
        ),
    }


@router.post("/agents/{agent_id}/reset-to-default")
def api_reset_to_default(
    agent_id: int,
    body: Optional[dict] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_master_admin),
):
    """Reset selected fields to the canonical version. Snapshots the current
    state as a new AgentVersion before reverting (so admins can roll back).

    Body: {"fields": ["system_prompt", "tools"]} — both default to True if
    body is omitted.
    """
    from app.services.langchain.canonical_registry import (
        is_registered as _is_in_registry,
        get_canonical_prompt,
        get_canonical_tools,
    )
    agent = db.query(CustomAgent).filter(CustomAgent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if not _is_in_registry(agent.agent_key):
        raise HTTPException(
            status_code=400,
            detail=f"Agent '{agent.agent_key}' is not in the canonical registry.",
        )

    fields = (body or {}).get("fields") or ["system_prompt", "tools"]

    # Build the update payload from canonical values.
    update_data: dict = {}
    if "system_prompt" in fields:
        canonical_prompt = get_canonical_prompt(agent.agent_key)
        if canonical_prompt is not None:
            update_data["system_prompt"] = canonical_prompt
    if "tools" in fields:
        # Tools field on CustomAgent is list-of-{tool_id, config}; resolving
        # canonical tool_keys back to AgentTool ids for write.
        canonical_keys = get_canonical_tools(agent.agent_key)
        if canonical_keys:
            tool_rows = (
                db.query(AgentTool)
                .filter(AgentTool.tool_key.in_(canonical_keys))
                .all()
            )
            update_data["tools"] = [
                {"tool_id": t.id, "config": {}} for t in tool_rows
            ]
        else:
            update_data["tools"] = []
    update_data["change_description"] = "Reset to system default (canonical)"

    updated = update_agent(db, agent_id, update_data, current_user.id)
    if not updated:
        raise HTTPException(status_code=500, detail="Reset failed")
    # update_agent's auto-flip logic should detect the reset and set
    # is_user_customized=False. Defensive: explicitly set in case the
    # canonical comparison fails (e.g. for tool-set differences).
    if getattr(updated, "is_user_customized", False):
        updated.is_user_customized = False
        db.commit()
        db.refresh(updated)
    return {
        "id": updated.id,
        "current_version": updated.current_version,
        "is_user_customized": getattr(updated, "is_user_customized", False),
        "status": "reset_to_default",
        "fields_reset": fields,
    }


@router.post("/agents/{agent_id}/validate-prompt")
def api_validate_prompt(
    agent_id: int,
    body: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_master_admin),
):
    """Live-validate a candidate system_prompt against the canonical registry's
    required placeholders + soft markers. Used by the frontend editor to show
    inline validation as the user types.
    """
    from app.services.langchain.canonical_registry import (
        is_registered as _is_in_registry,
        validate_user_prompt,
    )
    agent = db.query(CustomAgent).filter(CustomAgent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    candidate = body.get("system_prompt") or ""
    if not _is_in_registry(agent.agent_key):
        return {"valid": True, "missing_placeholders": [], "warnings": [], "registered": False}
    result = validate_user_prompt(agent.agent_key, candidate)
    result["registered"] = True
    return result


@router.delete("/agents/{agent_id}")
def api_delete_agent(
    agent_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_master_admin),
):
    """Delete an agent."""
    if not delete_agent(db, agent_id):
        raise HTTPException(status_code=404, detail="Agent not found or is a system agent")
    return {"status": "deleted"}


@router.post("/agents/{agent_id}/clone")
def api_clone_agent(
    agent_id: int,
    body: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_master_admin),
):
    """Clone an agent."""
    new_name = body.get("new_name", "")
    if not new_name:
        raise HTTPException(status_code=400, detail="new_name is required")
    agent = clone_agent(db, agent_id, new_name, current_user.id)
    if not agent:
        raise HTTPException(status_code=404, detail="Source agent not found")
    return {"id": agent.id, "agent_key": agent.agent_key, "status": "cloned"}


@router.post("/agents/{agent_id}/publish")
def api_publish_agent(
    agent_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_master_admin),
):
    """Publish agent to library."""
    agent = publish_agent(db, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    return {"id": agent.id, "is_published": agent.is_published, "status": "published"}


@router.get("/agents/{agent_id}/versions")
def api_get_versions(
    agent_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_master_admin),
):
    """List agent version history."""
    versions = get_agent_versions(db, agent_id)
    return [
        {
            "id": v.id, "version_number": v.version_number,
            "change_description": v.change_description,
            "system_prompt": v.system_prompt, "tools": v.tools,
            "model": v.model, "temperature": v.temperature,
            "max_tokens": v.max_tokens,
            "orchestration_config": v.orchestration_config,
            "created_by": v.created_by, "created_at": v.created_at,
        }
        for v in versions
    ]


@router.post("/agents/{agent_id}/rollback/{version_number}")
def api_rollback_agent(
    agent_id: int,
    version_number: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_master_admin),
):
    """Rollback agent to a previous version."""
    agent = rollback_agent(db, agent_id, version_number, current_user.id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent or version not found")
    return {"id": agent.id, "current_version": agent.current_version, "status": "rolled_back"}


# --- Agent Execution ---

@router.post("/agents/{agent_id}/execute")
async def api_execute_agent(
    agent_id: int,
    body: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_master_admin),
):
    """Execute an agent with given input."""
    input_data = body.get("input_data", {})
    try:
        result = await execute_agent(db, agent_id, input_data, current_user.id)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/agents/{agent_id}/execute-with-files")
async def api_execute_agent_with_files(
    agent_id: int,
    input_json: str = Form("{}"),
    files: List[UploadFile] = File(default=[]),
    doc_ids: str = Form(""),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_master_admin),
):
    """
    Execute an agent with document context.
    Accepts new file uploads AND/OR existing stored document IDs.
    New uploads are saved to disk + DB before execution.
    """
    import json
    try:
        input_data = json.loads(input_json)
    except json.JSONDecodeError:
        input_data = {"input": input_json}

    # Upload any new files and collect their IDs
    new_doc_ids = []
    for f in files:
        if not f.filename:
            continue
        try:
            file_data = await f.read()
            doc = upload_test_document(db, agent_id, f.filename, file_data, current_user.id)
            new_doc_ids.append(doc.id)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Failed to upload {f.filename}: {str(e)}")

    # Parse existing doc IDs from form field
    existing_ids = []
    if doc_ids:
        try:
            existing_ids = [int(x.strip()) for x in doc_ids.split(",") if x.strip()]
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid doc_ids format - expected comma-separated integers")

    # Combine all document IDs
    all_doc_ids = existing_ids + new_doc_ids

    # Build document context and inject into input_data
    if all_doc_ids:
        doc_context = build_documents_context(db, agent_id, doc_ids=all_doc_ids)
        if doc_context["uploaded_documents"]:
            input_data["uploaded_documents"] = doc_context["uploaded_documents"]
            input_data["uploaded_file_names"] = doc_context["uploaded_file_names"]

    try:
        result = await execute_agent(db, agent_id, input_data, current_user.id)
        # Include newly uploaded doc IDs in response so frontend can update its list
        if new_doc_ids:
            result["new_document_ids"] = new_doc_ids
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/agents/{agent_id}/stream")
async def api_stream_agent(
    agent_id: int,
    body: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_master_admin),
):
    """
    Stream an OpenAI agent execution via Server-Sent Events (SSE).
    Only available for agents with agent_type='openai_agent'.
    """
    from app.models.agent_builder import CustomAgent

    agent = db.query(CustomAgent).filter(CustomAgent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if not agent.is_enabled:
        raise HTTPException(status_code=400, detail="Agent is disabled")
    if agent.agent_type != "openai_agent":
        raise HTTPException(
            status_code=400,
            detail=f"Streaming only available for openai_agent type, got '{agent.agent_type}'",
        )

    from app.services.openai_agents.execution_service import stream_openai_agent

    input_data = body.get("input_data", {})
    session_id = body.get("session_id")

    return StreamingResponse(
        stream_openai_agent(
            db=db,
            agent=agent,
            input_data=input_data,
            user_id=current_user.id,
            session_id=session_id,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/agents/{agent_id}/executions/{execution_id}/trace")
def api_get_execution_trace(
    agent_id: int,
    execution_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_master_admin),
):
    """
    Get the trace details for an agent execution.
    Returns tool calls, handoffs, token usage, and timing per step.
    """
    execution = get_execution(db, execution_id)
    if not execution:
        raise HTTPException(status_code=404, detail="Execution not found")
    if execution.agent_id != agent_id:
        raise HTTPException(status_code=404, detail="Execution not found for this agent")

    metadata = execution.metadata_json or {}

    return {
        "execution_id": execution.id,
        "agent_id": agent_id,
        "status": execution.status,
        "engine": metadata.get("engine", "unknown"),
        "model": metadata.get("model"),
        "trigger": execution.trigger,
        "latency_ms": execution.latency_ms,
        "tokens_input": execution.tokens_input,
        "tokens_output": execution.tokens_output,
        "cost_estimate": execution.cost_estimate,
        "tool_calls": metadata.get("tool_calls", []),
        "handoffs_used": metadata.get("handoffs_used", False),
        "max_turns": metadata.get("max_turns"),
        "guardrail_warnings": metadata.get("guardrail_warnings", []),
        "session_id": metadata.get("session_id"),
        "openai_response_id": metadata.get("openai_response_id"),
        "errors": [execution.error_message] if execution.error_message else [],
        "created_at": execution.created_at.isoformat() if execution.created_at else None,
    }


# --- Human-in-the-Loop Tool Approval ---

@router.get("/approvals/pending")
def api_get_pending_approvals(
    execution_id: Optional[int] = Query(None),
    agent_key: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_master_admin),
):
    """Get all pending tool approval requests."""
    from app.services.openai_agents.human_in_the_loop import get_pending_approvals
    return get_pending_approvals(execution_id=execution_id, agent_key=agent_key)


@router.get("/approvals/{approval_id}")
def api_get_approval(
    approval_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_master_admin),
):
    """Get a specific approval request by ID."""
    from app.services.openai_agents.human_in_the_loop import get_approval
    approval = get_approval(approval_id)
    if not approval:
        raise HTTPException(status_code=404, detail="Approval not found")
    return approval


@router.post("/approvals/{approval_id}/resolve")
def api_resolve_approval(
    approval_id: str,
    body: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_master_admin),
):
    """
    Approve or reject a pending tool call.

    Body: {"approved": true/false}
    """
    from app.services.openai_agents.human_in_the_loop import resolve_approval

    approved = body.get("approved", False)
    result = resolve_approval(approval_id, approved=approved, resolved_by=current_user.id)
    if not result:
        raise HTTPException(status_code=404, detail="Approval not found")
    return result


# --- Test Documents ---

@router.post("/agents/{agent_id}/test-documents")
async def api_upload_test_document(
    agent_id: int,
    files: List[UploadFile] = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_master_admin),
):
    """Upload one or more test documents for an agent. Files are saved and text extracted."""
    uploaded = []
    for f in files:
        if not f.filename:
            continue
        try:
            file_data = await f.read()
            doc = upload_test_document(db, agent_id, f.filename, file_data, current_user.id)
            uploaded.append({
                "id": doc.id,
                "file_name": doc.file_name,
                "file_size": doc.file_size,
                "mime_type": doc.mime_type,
                "page_count": doc.page_count,
                "extraction_status": doc.extraction_status,
                "extraction_method": doc.extraction_method,
                "extraction_error": doc.extraction_error,
                "has_text": bool(doc.extracted_text),
                "uploaded_at": doc.uploaded_at.isoformat() if doc.uploaded_at else None,
            })
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to process {f.filename}: {str(e)}")
    return {"documents": uploaded, "count": len(uploaded)}


@router.get("/agents/{agent_id}/test-documents")
def api_list_test_documents(
    agent_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_master_admin),
):
    """List all test documents for an agent."""
    docs = list_test_documents(db, agent_id)
    return [
        {
            "id": d.id,
            "file_name": d.file_name,
            "file_size": d.file_size,
            "mime_type": d.mime_type,
            "page_count": d.page_count,
            "extraction_status": d.extraction_status,
            "extraction_method": d.extraction_method,
            "extraction_error": d.extraction_error,
            "has_text": bool(d.extracted_text),
            "text_preview": (d.extracted_text[:200] + "...") if d.extracted_text and len(d.extracted_text) > 200 else d.extracted_text,
            "uploaded_at": d.uploaded_at.isoformat() if d.uploaded_at else None,
        }
        for d in docs
    ]


@router.get("/test-documents/{doc_id}")
def api_get_test_document(
    doc_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_master_admin),
):
    """Get a single test document with full extracted text."""
    doc = get_test_document(db, doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    return {
        "id": doc.id,
        "agent_id": doc.agent_id,
        "file_name": doc.file_name,
        "file_size": doc.file_size,
        "mime_type": doc.mime_type,
        "page_count": doc.page_count,
        "extraction_status": doc.extraction_status,
        "extraction_method": doc.extraction_method,
        "extraction_error": doc.extraction_error,
        "extracted_text": doc.extracted_text,
        "uploaded_at": doc.uploaded_at.isoformat() if doc.uploaded_at else None,
    }


@router.delete("/test-documents/{doc_id}")
def api_delete_test_document(
    doc_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_master_admin),
):
    """Delete a single test document."""
    if not delete_test_document(db, doc_id):
        raise HTTPException(status_code=404, detail="Document not found")
    return {"status": "deleted"}


@router.delete("/agents/{agent_id}/test-documents")
def api_delete_all_test_documents(
    agent_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_master_admin),
):
    """Delete all test documents for an agent."""
    count = delete_all_test_documents(db, agent_id)
    return {"status": "deleted", "count": count}


@router.post("/test-documents/{doc_id}/re-extract")
def api_re_extract_document(
    doc_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_master_admin),
):
    """Re-run text extraction on a document."""
    doc = re_extract_text(db, doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found or file missing")
    return {
        "id": doc.id,
        "extraction_status": doc.extraction_status,
        "extraction_method": doc.extraction_method,
        "extraction_error": doc.extraction_error,
        "has_text": bool(doc.extracted_text),
        "page_count": doc.page_count,
    }


@router.get("/agents/{agent_id}/executions")
def api_get_executions(
    agent_id: int,
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_master_admin),
):
    """Get agent execution history."""
    executions = get_agent_executions(db, agent_id, limit)
    return [
        {
            "id": e.id, "agent_version": e.agent_version, "trigger": e.trigger,
            "status": e.status, "input_summary": e.input_summary,
            "output_summary": (e.output_summary or "")[:200],
            "tokens_input": e.tokens_input, "tokens_output": e.tokens_output,
            "latency_ms": e.latency_ms, "cost_estimate": e.cost_estimate,
            "error_message": e.error_message, "created_at": e.created_at,
        }
        for e in executions
    ]


@router.get("/executions/{execution_id}")
def api_get_execution_detail(
    execution_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_master_admin),
):
    """Get single execution detail."""
    execution = get_execution(db, execution_id)
    if not execution:
        raise HTTPException(status_code=404, detail="Execution not found")
    return {
        "id": execution.id, "agent_id": execution.agent_id,
        "agent_version": execution.agent_version, "trigger": execution.trigger,
        "status": execution.status, "input_summary": execution.input_summary,
        "output_summary": execution.output_summary,
        "tokens_input": execution.tokens_input, "tokens_output": execution.tokens_output,
        "latency_ms": execution.latency_ms, "cost_estimate": execution.cost_estimate,
        "error_message": execution.error_message,
        "parent_execution_id": execution.parent_execution_id,
        "metadata_json": execution.metadata_json,
        "created_at": execution.created_at,
    }


@router.get("/agents/{agent_id}/stats")
def api_get_agent_stats(
    agent_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_master_admin),
):
    """Get execution statistics for an agent."""
    return get_execution_stats(db, agent_id)


# --- Agent Testing ---

@router.get("/agents/{agent_id}/tests")
def api_get_test_cases(
    agent_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_master_admin),
):
    """List test cases for an agent."""
    tests = get_test_cases(db, agent_id)
    return [
        {
            "id": t.id, "test_name": t.test_name, "input_data": t.input_data,
            "expected_output": t.expected_output,
            "evaluation_criteria": t.evaluation_criteria,
            "is_active": t.is_active, "created_at": t.created_at,
        }
        for t in tests
    ]


@router.post("/agents/{agent_id}/tests")
def api_create_test(
    agent_id: int,
    body: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_master_admin),
):
    """Create a test case."""
    test = create_test_case(db, agent_id, body, current_user.id)
    return {"id": test.id, "test_name": test.test_name, "status": "created"}


@router.put("/tests/{test_id}")
def api_update_test(
    test_id: int,
    body: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_master_admin),
):
    """Update a test case."""
    test = update_test_case(db, test_id, body)
    if not test:
        raise HTTPException(status_code=404, detail="Test case not found")
    return {"id": test.id, "status": "updated"}


@router.delete("/tests/{test_id}")
def api_delete_test(
    test_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_master_admin),
):
    """Delete a test case."""
    if not delete_test_case(db, test_id):
        raise HTTPException(status_code=404, detail="Test case not found")
    return {"status": "deleted"}


@router.post("/tests/{test_id}/run")
async def api_run_test(
    test_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_master_admin),
):
    """Run a single test case."""
    try:
        result = await run_test_case(db, test_id, current_user.id)
        return {
            "id": result.id, "passed": result.passed, "score": result.score,
            "output_text": (result.output_text or "")[:500],
            "latency_ms": result.latency_ms, "cost_estimate": result.cost_estimate,
            "error_message": result.error_message,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/agents/{agent_id}/tests/run-all")
async def api_run_all_tests(
    agent_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_master_admin),
):
    """Run all test cases for an agent."""
    results = await run_all_tests(db, agent_id, current_user.id)
    return [
        {
            "id": r.id, "test_case_id": r.test_case_id,
            "passed": r.passed, "score": r.score,
            "latency_ms": r.latency_ms, "error_message": r.error_message,
        }
        for r in results
    ]


@router.post("/agents/{agent_id}/compare-versions")
async def api_compare_versions(
    agent_id: int,
    body: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_master_admin),
):
    """Compare two agent versions by running tests against both."""
    # This would be a more complex operation - for now return a placeholder
    return {"status": "not_implemented", "message": "Version comparison coming soon"}


# --- Tools ---

@router.get("/tools")
def api_list_tools(
    tool_type: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_master_admin),
):
    """List available tools. Cached in Redis (invalidated on tool CRUD)."""
    from app.core.redis_client import cache_get_json, cache_set_json, cache_key
    ck = cache_key("agent_tools", tool_type or "all")
    cached = cache_get_json(ck)
    if cached is not None:
        return cached

    tools = list_tools(db, tool_type=tool_type)
    result = [
        {
            "id": t.id, "tool_key": t.tool_key, "display_name": t.display_name,
            "description": t.description, "tool_type": t.tool_type,
            "config_schema": t.config_schema or {}, "default_config": t.default_config or {},
            "is_system": t.is_system, "is_active": t.is_active,
            "created_at": t.created_at,
        }
        for t in tools
    ]
    cache_set_json(ck, result)
    return result


@router.post("/tools")
def api_create_tool(
    body: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_master_admin),
):
    """Create a custom tool."""
    tool = create_tool(db, body, current_user.id)
    return {"id": tool.id, "tool_key": tool.tool_key, "status": "created"}


@router.put("/tools/{tool_id}")
def api_update_tool(
    tool_id: int,
    body: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_master_admin),
):
    """Update a tool."""
    tool = update_tool(db, tool_id, body)
    if not tool:
        raise HTTPException(status_code=404, detail="Tool not found")
    return {"id": tool.id, "status": "updated"}


@router.delete("/tools/{tool_id}")
def api_delete_tool(
    tool_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_master_admin),
):
    """Delete a tool (non-system only)."""
    if not delete_tool(db, tool_id):
        raise HTTPException(status_code=404, detail="Tool not found or is a system tool")
    return {"status": "deleted"}


@router.post("/tools/{tool_id}/test")
async def api_test_tool(
    tool_id: int,
    body: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_master_admin),
):
    """Test a tool with given input data."""
    import time
    tool_record = get_tool(db, tool_id)
    if not tool_record:
        raise HTTPException(status_code=404, detail="Tool not found")

    try:
        from app.services.langchain.tools.tool_loader import load_tools_by_keys
        tools = load_tools_by_keys(db, [tool_record.tool_key], agent_key="__test__")
        if not tools:
            raise HTTPException(status_code=400, detail=f"Tool '{tool_record.tool_key}' could not be loaded")

        tool_instance = tools[0]
        test_input = body.get("input", body.get("input_data", ""))
        if isinstance(test_input, dict):
            import json
            test_input = json.dumps(test_input)

        start = time.time()
        result = await tool_instance.arun(test_input) if hasattr(tool_instance, 'arun') else tool_instance.run(test_input)
        latency_ms = int((time.time() - start) * 1000)

        return {
            "output": str(result)[:5000],
            "latency_ms": latency_ms,
            "status": "success",
        }
    except Exception as e:
        return {
            "output": str(e),
            "latency_ms": 0,
            "status": "error",
        }


# --- Library ---

@router.get("/library")
def api_get_library(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_master_admin),
):
    """List published agents in the library."""
    agents = list_agents(db, is_published=True)
    return [
        {
            "id": a.id, "agent_key": a.agent_key, "display_name": a.display_name,
            "description": a.description, "agent_type": a.agent_type,
            "category": a.category, "tags": a.tags,
            "is_system": a.is_system,
        }
        for a in agents
    ]
