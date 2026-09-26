"""
DRPL Backend - Agent Tools Service
Tool registry for agent builder: CRUD operations and system tool seeding.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.models.agent_builder import AgentTool

logger = logging.getLogger(__name__)


# --- Built-in system tools ---

SYSTEM_TOOLS = [
    # ── Tools that are implemented in tool_loader but had no agent_tools row ──
    # Without a row they cannot be assigned to an agent from the Agent Builder
    # and cannot appear in an agent's `tools` column, so the code implementing
    # them was unreachable. Four of these are costing_researcher's core
    # research tools — that agent was running with nothing to research with.
    {
        "tool_key": "anonymizing_web_search",
        "display_name": "Anonymizing Web Search",
        "description": (
            "Web search that strips tender-identifying details from the query "
            "before sending it, so rate research cannot leak which tender is "
            "being priced."
        ),
        "tool_type": "web_search",
        "handler_module": "app.services.langchain.tools.anonymizing_web_search_tool",
        "config_schema": {"type": "object", "properties": {}},
    },
    {
        "tool_key": "ratecard_lookup",
        "display_name": "Rate Card Lookup",
        "description": "Looks up known unit rates from DRPL's stored rate cards.",
        "tool_type": "db_query",
        "handler_module": "app.services.langchain.tools.ratecard_lookup_tool",
        "config_schema": {"type": "object", "properties": {}},
    },
    {
        "tool_key": "costing_training_retrieval",
        "display_name": "Costing Training Retrieval",
        "description": (
            "Retrieves comparable priced lines from DRPL's historical costing "
            "training data."
        ),
        "tool_type": "db_query",
        "handler_module": "app.services.langchain.tools.costing_training_retrieval_tool",
        "config_schema": {"type": "object", "properties": {}},
    },
    {
        "tool_key": "delegate_travel_research",
        "display_name": "Delegate Travel Research",
        "description": "Hands crew travel and lodging cost research to a sub-agent.",
        "tool_type": "delegation",
        "handler_module": "app.services.langchain.tools.costing_training_retrieval_tool",
        "config_schema": {"type": "object", "properties": {}},
    },
    {
        "tool_key": "cost_calculator",
        "display_name": "Cost Calculator",
        "description": (
            "Deterministic arithmetic for cost lines — quantities, unit rates, "
            "totals and GST. Keeps money out of the model's hands."
        ),
        "tool_type": "computation",
        "handler_module": "app.services.langchain.tools.cost_calculator_tool",
        "config_schema": {"type": "object", "properties": {}},
    },
    {
        "tool_key": "tender_scoring_status",
        "display_name": "Tender Scoring Status",
        "description": "Reports where a tender sits in the automatic scoring pipeline.",
        "tool_type": "db_query",
        "handler_module": "app.services.langchain.tools.scoring_status_tool",
        "config_schema": {"type": "object", "properties": {}},
    },

    {
        "tool_key": "document_reader",
        "display_name": "Document Reader",
        "description": "Reads and extracts text from uploaded tender documents (PDF, DOCX, images with OCR)",
        "tool_type": "document_op",
        "handler_module": "app.services.document_parser",
        "config_schema": {
            "type": "object",
            "properties": {
                "max_pages": {"type": "integer", "default": 100},
                "ocr_enabled": {"type": "boolean", "default": True},
            },
        },
    },
    {
        "tool_key": "tender_lookup",
        "display_name": "Tender Lookup",
        "description": "Queries the tender database by ID, portal, keyword, or filters",
        "tool_type": "db_query",
        "handler_module": "app.services.tender_service",
        "config_schema": {
            "type": "object",
            "properties": {
                "max_results": {"type": "integer", "default": 20},
                "include_documents": {"type": "boolean", "default": False},
            },
        },
    },
    {
        "tool_key": "checklist_reader",
        "display_name": "Checklist Reader",
        "description": "Reads and parses compliance checklists from tender documents",
        "tool_type": "document_op",
        "handler_module": "app.services.checklist_service",
        "config_schema": {
            "type": "object",
            "properties": {
                "format": {"type": "string", "enum": ["json", "markdown"], "default": "json"},
            },
        },
    },
    {
        "tool_key": "web_search",
        "display_name": "Web Search",
        "description": "Searches the web for additional context on tenders, organizations, and requirements",
        "tool_type": "web_search",
        "handler_module": "app.services.langchain.tools.web_search_tool",
        "config_schema": {
            "type": "object",
            "properties": {
                "max_results": {"type": "integer", "default": 5},
                "safe_search": {"type": "boolean", "default": True},
            },
        },
    },
    {
        "tool_key": "document_generator",
        "display_name": "Document Generator",
        "description": "Generates professional documents (proposals, cost statements, letters, certificates) with AI content generation, letterhead templates, and digital signatures",
        "tool_type": "document_op",
        "handler_module": "app.services.document_ai_service",
        "config_schema": {
            "type": "object",
            "properties": {
                "document_type": {
                    "type": "string",
                    "enum": ["proposal", "cost_statement", "letter", "certificate", "custom"],
                    "default": "custom",
                },
                "include_signatures": {"type": "boolean", "default": False},
                "letterhead_template_id": {"type": "integer", "default": None},
                "mode": {
                    "type": "string",
                    "enum": ["generate", "enhance"],
                    "default": "generate",
                },
            },
        },
    },
    # --- LangChain Memory & MCP Tools ---
    {
        "tool_key": "memory_store",
        "display_name": "Memory Store",
        "description": "Stores facts, learnings, preferences, and decisions in long-term agent memory for future reference across sessions",
        "tool_type": "memory",
        "handler_module": "app.services.langchain.tools.memory_tool",
        "config_schema": {
            "type": "object",
            "properties": {
                "default_importance": {"type": "number", "default": 0.5},
            },
        },
    },
    {
        "tool_key": "memory_retrieve",
        "display_name": "Memory Retrieve",
        "description": "Searches and retrieves relevant information from long-term agent memory using keyword-based scoring",
        "tool_type": "memory",
        "handler_module": "app.services.langchain.tools.memory_tool",
        "config_schema": {
            "type": "object",
            "properties": {
                "default_top_k": {"type": "integer", "default": 10},
            },
        },
    },
    {
        "tool_key": "semantic_search",
        "display_name": "Semantic Search",
        "description": "Searches across all embedded tender documents and RAG corpus using vector-based semantic similarity. Returns the most relevant text chunks ranked by relevance score. Optionally scoped to a specific tender's documents.",
        "tool_type": "db_query",
        "handler_module": "app.services.langchain.tools.semantic_search_tool",
        "config_schema": {
            "type": "object",
            "properties": {
                "default_top_k": {"type": "integer", "default": 5},
                "score_threshold": {"type": "number", "default": 0.3},
            },
        },
    },
    {
        "tool_key": "mcp_bridge",
        "display_name": "MCP Bridge",
        "description": "Bridges to external MCP (Model Context Protocol) servers, allowing agents to call tools hosted on remote MCP servers",
        "tool_type": "mcp",
        "handler_module": "app.services.mcp.mcp_client",
        "config_schema": {
            "type": "object",
            "properties": {
                "server_names": {"type": "array", "items": {"type": "string"}, "default": []},
            },
        },
    },
    # --- Provider-Specific Tools ---
    {
        "tool_key": "code_execution",
        "display_name": "Code Execution",
        "description": "Execute Python code for calculations, data processing, and verification using Google Gemini's sandboxed code execution. Ideal for cost calculations, unit conversions, and data validation.",
        "tool_type": "code_exec",
        "handler_module": "app.services.langchain.tools.code_execution_tool",
        "config_schema": {
            "type": "object",
            "properties": {
                "gemini_model": {"type": "string", "default": "gemini-2.5-flash"},
            },
        },
    },
    {
        "tool_key": "structured_output",
        "display_name": "Structured Output",
        "description": "Generate structured JSON output conforming to a specified schema. Use for extracting structured data, creating cost breakdowns, checklists, or any structured format.",
        "tool_type": "generation",
        "handler_module": "app.services.langchain.tools.structured_output_tool",
        "config_schema": {
            "type": "object",
            "properties": {
                "temperature": {"type": "number", "default": 0.1},
            },
        },
    },
    # --- OpenAI Hosted Tools (run server-side on OpenAI infrastructure) ---
    {
        "tool_key": "openai_web_search",
        "display_name": "OpenAI Web Search",
        "description": "Real-time web search powered by OpenAI. Runs server-side — no external API key needed beyond OpenAI.",
        "tool_type": "web_search",
        "handler_module": "app.services.openai_agents.hosted_tools",
        "config_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "tool_key": "openai_code_interpreter",
        "display_name": "OpenAI Code Interpreter",
        "description": "Sandboxed Python code execution via OpenAI. Can process uploaded files, generate charts, and run calculations.",
        "tool_type": "code_exec",
        "handler_module": "app.services.openai_agents.hosted_tools",
        "config_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "tool_key": "openai_file_search",
        "display_name": "OpenAI File Search",
        "description": "Document analysis and retrieval via OpenAI's vector store. Upload documents and query them with natural language.",
        "tool_type": "document_op",
        "handler_module": "app.services.openai_agents.hosted_tools",
        "config_schema": {
            "type": "object",
            "properties": {
                "vector_store_ids": {"type": "array", "items": {"type": "string"}, "default": []},
            },
        },
    },
    # --- Canvas Workspace Tools ---
    {
        "tool_key": "workspace_init",
        "display_name": "Workspace Initialize",
        "description": "Initialize a canvas workspace for a tender. Creates document workspace entries for each checklist item with auto-matched format templates.",
        "tool_type": "document_op",
        "handler_module": "app.services.langchain.tools.workspace_tools",
        "config_schema": {"type": "object", "properties": {}},
    },
    {
        "tool_key": "workspace_status",
        "display_name": "Workspace Status",
        "description": "Check the current status and progress of a tender's canvas workspace. Shows completion stats, items by status, and category breakdown.",
        "tool_type": "document_op",
        "handler_module": "app.services.langchain.tools.workspace_tools",
        "config_schema": {"type": "object", "properties": {}},
    },
    {
        "tool_key": "workspace_list_items",
        "display_name": "Workspace List Items",
        "description": "List all document items in a tender's workspace with their review status, content version, and assigned agent.",
        "tool_type": "document_op",
        "handler_module": "app.services.langchain.tools.workspace_tools",
        "config_schema": {"type": "object", "properties": {}},
    },
    {
        "tool_key": "workspace_generate_document",
        "display_name": "Workspace Generate Document",
        "description": "Generate content for a specific document in the workspace using its assigned AI agent and format template.",
        "tool_type": "document_op",
        "handler_module": "app.services.langchain.tools.workspace_tools",
        "config_schema": {"type": "object", "properties": {}},
    },
    {
        "tool_key": "xlsx_generator",
        "display_name": "XLSX Generator",
        "description": (
            "Generate a formatted Excel (.xlsx) cost-breakdown workbook from row "
            "data. Produces bold headers, currency-formatted columns, totals row, "
            "and frozen header pane. Persists as a downloadable artifact."
        ),
        "tool_type": "document_op",
        "handler_module": "app.services.langchain.tools.xlsx_generator_tool",
        "config_schema": {"type": "object", "properties": {}},
    },
    {
        "tool_key": "clarify",
        "display_name": "Clarify (Ask User)",
        "description": (
            "Pause the run and ask the user a targeted clarification question. "
            "Queues a PendingClarification row; the streaming handler surfaces "
            "it to the UI as a toast + modal. The agent stops after calling."
        ),
        "tool_type": "interaction",
        "handler_module": "app.services.langchain.tools.clarify_tool",
        "config_schema": {"type": "object", "properties": {}},
    },
    {
        "tool_key": "web_fetch",
        "display_name": "Web Fetch",
        "description": (
            "Fetch a single http(s) URL and return its readable main content "
            "as Markdown (via httpx + trafilatura). Use for specific pages — "
            "tender notices, articles, portal listings — not broad search."
        ),
        "tool_type": "web",
        "handler_module": "app.services.langchain.tools.web_fetch_tool",
        "config_schema": {"type": "object", "properties": {}},
    },
    {
        "tool_key": "advisor",
        "display_name": "Advisor (Second Opinion)",
        "description": (
            "Consult a senior adviser (Claude with extended thinking) for a "
            "reasoned second opinion on a hard sub-problem: ambiguous clauses, "
            "quote comparisons, proposal framing. Use sparingly — this is "
            "more expensive than a normal model call."
        ),
        "tool_type": "ai",
        "handler_module": "app.services.langchain.tools.advisor_tool",
        "config_schema": {"type": "object", "properties": {}},
    },
    {
        "tool_key": "docx_generator",
        "display_name": "DOCX Generator",
        "description": (
            "Generate an editable Word (.docx) document from structured "
            "sections (heading/level/body). Use for proposal bodies, cover "
            "letters, compliance statements where the recipient wants an "
            "editable source file. Persists as a downloadable artifact."
        ),
        "tool_type": "document_op",
        "handler_module": "app.services.langchain.tools.docx_generator_tool",
        "config_schema": {"type": "object", "properties": {}},
    },
]


def _extend_from_capability_registry(tools: list) -> list:
    """Append any runnable capability the literal list above does not cover.

    The literal list stays because it carries hand-written config schemas and
    rows for tools outside the class registry (the OpenAI path's file_search,
    for example). But it must never again be the reason a runnable tool cannot
    be assigned — that is exactly how costing_researcher showed
    'Assigned Tools (0)' while its code declared eight. Any `kind="class"`
    capability missing here is generated from its registry entry, so
    runnable ⇒ assignable holds structurally, not by review.
    """
    from app.services.langchain.capability_registry import CAPABILITIES

    seeded = {t["tool_key"] for t in tools}
    for key, cap in CAPABILITIES.items():
        if cap.kind != "class" or key in seeded:
            continue
        module_path, _attr = cap.target.split(":")
        tools.append({
            "tool_key": key,
            "display_name": key.replace("_", " ").title(),
            "description": cap.purpose,
            "tool_type": cap.domain,
            "handler_module": module_path,
            "config_schema": {"type": "object", "properties": {}},
        })
    return tools


SYSTEM_TOOLS = _extend_from_capability_registry(SYSTEM_TOOLS)



# --- CRUD ---

_TOOLS_CACHE_PREFIX = "drpl:cache:agent_tools"


def _invalidate_tools_cache() -> None:
    """Drop every cached variant of list_tools (tool_type filters included)."""
    try:
        from app.core.redis_client import cache_delete_prefix
        cache_delete_prefix(_TOOLS_CACHE_PREFIX)
    except Exception as e:
        logger.debug(f"tools cache invalidate: {e}")


def create_tool(db: Session, data: dict, user_id: Optional[int] = None) -> AgentTool:
    """
    Create a new agent tool.

    Args:
        db: Database session.
        data: Dict with tool_key, display_name, description, tool_type, etc.
        user_id: ID of the creating user.

    Returns:
        The newly created AgentTool.
    """
    tool = AgentTool(
        tool_key=data["tool_key"],
        display_name=data["display_name"],
        description=data.get("description"),
        tool_type=data["tool_type"],
        config_schema=data.get("config_schema", {}),
        default_config=data.get("default_config"),
        handler_module=data.get("handler_module"),
        is_system=data.get("is_system", False),
        is_active=data.get("is_active", True),
        created_by=user_id,
    )
    db.add(tool)
    db.commit()
    db.refresh(tool)
    _invalidate_tools_cache()
    logger.info(f"Created tool '{tool.tool_key}' (id={tool.id})")
    return tool


def update_tool(db: Session, tool_id: int, data: dict) -> Optional[AgentTool]:
    """
    Update an existing tool.

    Returns:
        The updated AgentTool, or None if not found.
    """
    tool = db.query(AgentTool).filter(AgentTool.id == tool_id).first()
    if not tool:
        return None

    for key, value in data.items():
        if hasattr(tool, key) and key not in ("id", "tool_key", "created_by", "created_at"):
            setattr(tool, key, value)

    tool.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(tool)
    _invalidate_tools_cache()
    logger.info(f"Updated tool '{tool.tool_key}' (id={tool_id})")
    return tool


def list_tools(db: Session, tool_type: Optional[str] = None) -> list[AgentTool]:
    """List all tools, optionally filtered by type."""
    query = db.query(AgentTool)
    if tool_type is not None:
        query = query.filter(AgentTool.tool_type == tool_type)
    return query.order_by(AgentTool.display_name).all()


def get_tool(db: Session, tool_id: int) -> Optional[AgentTool]:
    """Get a single tool by ID."""
    return db.query(AgentTool).filter(AgentTool.id == tool_id).first()


def delete_tool(db: Session, tool_id: int) -> bool:
    """
    Delete a tool. System tools cannot be deleted.

    Returns:
        True if deleted, False if not found or is a system tool.
    """
    tool = db.query(AgentTool).filter(AgentTool.id == tool_id).first()
    if not tool:
        return False
    if tool.is_system:
        logger.warning(f"Attempted to delete system tool '{tool.tool_key}' -- denied")
        return False
    db.delete(tool)
    db.commit()
    _invalidate_tools_cache()
    logger.info(f"Deleted tool '{tool.tool_key}' (id={tool_id})")
    return True


def _defaults_from_schema(config_schema: dict) -> dict:
    """Extract default values from a JSON Schema's properties."""
    props = config_schema.get("properties", {})
    return {k: v["default"] for k, v in props.items() if "default" in v}


def seed_system_tools(db: Session) -> int:
    """
    Seed built-in system tools into the agent_tools table.

    Creates missing tools and backfills default_config on existing ones.

    Returns:
        Number of newly created tools.
    """
    created = 0
    for defaults in SYSTEM_TOOLS:
        schema = defaults.get("config_schema", {})
        computed_defaults = _defaults_from_schema(schema)

        existing = db.query(AgentTool).filter(AgentTool.tool_key == defaults["tool_key"]).first()
        if existing:
            # Backfill default_config if missing
            if not existing.default_config and computed_defaults:
                existing.default_config = computed_defaults
                existing.updated_at = datetime.now(timezone.utc)
            continue

        tool_data = {
            **defaults,
            "default_config": computed_defaults or {},
            "is_system": True,
            "is_active": True,
        }
        create_tool(db, tool_data, user_id=None)
        created += 1

    db.commit()
    logger.info(f"Seeded {created} system tools ({len(SYSTEM_TOOLS) - created} already existed)")
    return created
