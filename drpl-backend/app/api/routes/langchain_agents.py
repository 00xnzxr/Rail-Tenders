"""
DRPL Backend - LangChain Agent API Routes
Endpoints for LangChain pipeline execution, agent chat, memory management,
and MCP server configuration.
"""

import json
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, UploadFile, File, Form
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.auth import get_current_user
from app.services.budget_service import assert_within_budget
from app.models.user import User

router = APIRouter(prefix="/langchain", tags=["langchain-agents"])


# ──────────────────────────────────────────────
# Pipeline Endpoints
# ──────────────────────────────────────────────

@router.post("/pipeline/{tender_id}/run")
async def run_pipeline(
    tender_id: int,
    body: dict = {},
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Start the full tender processing pipeline (async)."""
    from app.services.langchain.langchain_execution_service import run_tender_pipeline

    result = await run_tender_pipeline(
        db=db,
        tender_id=tender_id,
        user_id=current_user.id,
        letterhead_template_id=body.get("letterhead_template_id"),
        signature_ids=body.get("signature_ids"),
        steps=body.get("steps"),
    )
    return result


@router.post("/pipeline/{tender_id}/run-step")
async def run_pipeline_step(
    tender_id: int,
    body: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Run a specific pipeline step for a tender."""
    step = body.get("step")
    if not step:
        raise HTTPException(status_code=400, detail="'step' is required")

    valid_steps = ["analyze_documents", "generate_checklist", "generate_documents", "research_costing"]
    if step not in valid_steps:
        raise HTTPException(status_code=400, detail=f"Invalid step. Must be one of: {valid_steps}")

    if step == "analyze_documents":
        from app.services.langchain.graphs.document_analysis_agent import run_document_analysis
        result = await run_document_analysis(db, tender_id, user_id=current_user.id)
        return result

    elif step == "generate_checklist":
        from app.services.checklist_service import generate_checklist
        items = await generate_checklist(db, tender_id)
        return {"items": [{"name": i.item_name, "required": i.is_required} for i in items]}

    elif step == "generate_documents":
        from app.services.langchain.graphs.proposal_agent import run_proposal_generation
        # Need analysis and checklist first
        from app.models.checklist import ChecklistItem
        items = db.query(ChecklistItem).filter(ChecklistItem.tender_id == tender_id).all()
        checklist = [{"name": i.item_name, "description": i.item_description, "is_required": i.is_required} for i in items]
        result = await run_proposal_generation(
            db=db, tender_id=tender_id, checklist_items=checklist,
            analysis_result=body.get("analysis_result", {}),
            letterhead_template_id=body.get("letterhead_template_id"),
            signature_ids=body.get("signature_ids"),
            user_id=current_user.id,
        )
        return {"generated_documents": result}

    elif step == "research_costing":
        from app.services.langchain.graphs.costing_agent import run_costing_research
        result = await run_costing_research(
            db=db, tender_id=tender_id,
            analysis_result=body.get("analysis_result", {}),
            user_id=current_user.id,
        )
        return result


@router.get("/pipeline/{tender_id}/status")
def get_pipeline_status(
    tender_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get the latest pipeline execution status for a tender."""
    from app.models.agent_builder import AgentExecution

    execution = db.query(AgentExecution).filter(
        AgentExecution.trigger == "pipeline",
        AgentExecution.input_summary.contains(f'"tender_id": {tender_id}'),
    ).order_by(AgentExecution.created_at.desc()).first()

    if not execution:
        return {"status": "not_started", "tender_id": tender_id}

    return {
        "execution_id": execution.id,
        "status": execution.status,
        "latency_ms": execution.latency_ms,
        "created_at": execution.created_at,
        "output_summary": execution.output_summary,
        "metadata": execution.metadata_json,
        "error": execution.error_message,
    }


# ──────────────────────────────────────────────
# Agent Chat Endpoints
# ──────────────────────────────────────────────


def _assert_conversation_access(db: Session, session_id: Optional[str], user: User) -> None:
    """404 unless ``user`` may read (and continue) conversation ``session_id``.

    These routes returned -- and appended to -- any session's transcript by
    id. A conversation behind a Command Center session belongs to that
    session's owner. Any other conversation with history is an Agent Builder
    test chat, a master_admin page, and stays with master_admin. A session id
    with no history yet is a new conversation and anyone may start it.
    """
    from app.core.roles import MASTER_ADMIN, normalize
    from app.models.agent_memory import AgentConversationHistory
    from app.models.proposal import ProposalSession

    if not session_id or normalize(user.role) == MASTER_ADMIN:
        return
    owner = (
        db.query(ProposalSession.created_by)
        .filter(ProposalSession.router_session_id == session_id)
        .first()
    )
    if owner is not None:
        if owner[0] != user.id:
            raise HTTPException(status_code=404, detail="Session not found")
        return
    has_history = (
        db.query(AgentConversationHistory.id)
        .filter(AgentConversationHistory.session_id == session_id)
        .first()
    )
    if has_history is not None:
        raise HTTPException(status_code=404, detail="Session not found")


@router.post("/agents/{agent_key}/chat")
async def chat_with_agent(
    agent_key: str,
    body: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Send a message to a LangChain agent and get a response with tool calls."""
    from app.models.agent_builder import CustomAgent
    from app.services.langchain.langchain_execution_service import execute_langchain_agent

    agent = db.query(CustomAgent).filter(CustomAgent.agent_key == agent_key).first()
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_key}' not found")
    if not agent.is_enabled:
        raise HTTPException(status_code=400, detail=f"Agent '{agent_key}' is disabled")

    message = body.get("message", "")
    if not message.strip():
        raise HTTPException(status_code=400, detail="Message is required")

    assert_within_budget(db, current_user)

    session_id = body.get("session_id", str(uuid.uuid4()))
    _assert_conversation_access(db, session_id, current_user)

    result = await execute_langchain_agent(
        db=db,
        agent=agent,
        input_data={"message": message},
        user_id=current_user.id,
        session_id=session_id,
    )

    return result


@router.get("/agents/{agent_key}/chat/{session_id}/history")
def get_chat_history(
    agent_key: str,
    session_id: str,
    limit: int = 50,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get conversation history for an agent chat session."""
    from app.services.langchain.memory_service import get_conversation_history

    _assert_conversation_access(db, session_id, current_user)

    history = get_conversation_history(db, session_id, limit=limit)
    return [
        {
            "id": h.id,
            "role": h.role,
            "content": h.content,
            "tool_calls": h.tool_calls,
            "created_at": h.created_at,
        }
        for h in history
    ]


# ──────────────────────────────────────────────
# Proposal Chat (Agent Router) Endpoints
# ──────────────────────────────────────────────

@router.post("/proposal-chat")
async def proposal_chat(
    body: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Route a message through the agent router and return the full response."""
    from app.services.langchain.graphs.agent_router_graph import route_and_execute

    message = body.get("message", "")
    if not message.strip():
        raise HTTPException(status_code=400, detail="Message is required")

    assert_within_budget(db, current_user)
    _assert_conversation_access(db, body.get("session_id"), current_user)

    result = await route_and_execute(
        db=db,
        message=message,
        session_id=body.get("session_id"),
        tender_id=body.get("tender_id"),
        user_id=current_user.id,
    )

    return result


@router.post("/proposal-chat/stream")
async def proposal_chat_stream(
    body: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Stream a routed agent response via Server-Sent Events (SSE)."""
    from app.services.langchain.streaming_handler import stream_router_response

    message = body.get("message", "")
    if not message.strip():
        raise HTTPException(status_code=400, detail="Message is required")

    assert_within_budget(db, current_user)
    _assert_conversation_access(db, body.get("session_id"), current_user)

    return StreamingResponse(
        stream_router_response(
            db=db,
            message=message,
            session_id=body.get("session_id"),
            tender_id=body.get("tender_id"),
            user_id=current_user.id,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/proposal-chat/{session_id}/history")
def get_proposal_chat_history(
    session_id: str,
    limit: int = 50,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get conversation history for a proposal chat session with routing metadata."""
    from app.services.langchain.memory_service import get_conversation_history

    _assert_conversation_access(db, session_id, current_user)

    history = get_conversation_history(db, session_id, limit=limit)
    return [
        {
            "id": h.id,
            "role": h.role,
            "content": h.content,
            "tool_calls": h.tool_calls,
            "output_type": h.output_type,
            "routed_from": h.routed_from,
            "metadata": h.metadata_json,
            "created_at": h.created_at,
        }
        for h in history
    ]


# ──────────────────────────────────────────────
# Memory Endpoints
# ──────────────────────────────────────────────


def _actor_of(user: User):
    from app.core.actor_context import Actor

    return Actor(user_id=user.id, role=user.role)


@router.get("/memory")
def list_agent_memories(
    agent_key: Optional[str] = None,
    memory_type: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List agent memories with optional filters."""
    from app.services.langchain.memory_service import list_memories

    memories = list_memories(db, agent_key=agent_key, memory_type=memory_type,
                             limit=limit, offset=offset, actor=_actor_of(current_user))
    return [
        {
            "id": m.id,
            "agent_key": m.agent_key,
            "memory_type": m.memory_type,
            "content": m.content,
            "context": m.context,
            "keywords": m.keywords,
            "importance": m.importance,
            "access_count": m.access_count,
            "tender_id": m.tender_id,
            "created_at": m.created_at,
            "last_accessed_at": m.last_accessed_at,
        }
        for m in memories
    ]


@router.post("/memory")
def create_memory(
    body: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Manually store a memory."""
    from app.services.langchain.memory_service import store_memory

    content = body.get("content", "")
    if not content.strip():
        raise HTTPException(status_code=400, detail="Content is required")

    memory = store_memory(
        db=db,
        agent_key=body.get("agent_key"),
        memory_type=body.get("memory_type", "fact"),
        content=content,
        keywords=body.get("keywords", []),
        importance=body.get("importance", 0.5),
        context=body.get("context"),
        tender_id=body.get("tender_id"),
        created_by=current_user.id,
    )

    return {"id": memory.id, "status": "stored"}


@router.delete("/memory/{memory_id}")
def delete_agent_memory(
    memory_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delete a memory by ID."""
    from app.services.langchain.memory_service import delete_memory

    if not delete_memory(db, memory_id, actor=_actor_of(current_user)):
        raise HTTPException(status_code=404, detail="Memory not found")
    return {"status": "deleted"}


@router.get("/memory/search")
def search_memories(
    query: str,
    agent_key: Optional[str] = None,
    top_k: int = 10,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Search memories by keyword relevance."""
    from app.services.langchain.memory_service import retrieve_memories

    from app.core.actor_context import actor_scope

    with actor_scope(user_id=current_user.id, role=current_user.role):
        memories = retrieve_memories(db, agent_key=agent_key, query=query, top_k=top_k)
    return [
        {
            "id": m.id,
            "agent_key": m.agent_key,
            "memory_type": m.memory_type,
            "content": m.content,
            "keywords": m.keywords,
            "importance": m.importance,
            "created_at": m.created_at,
        }
        for m in memories
    ]


@router.get("/memory/stats")
def get_memory_stats(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get aggregate memory statistics."""
    from app.services.langchain.memory_service import get_memory_stats as _get_stats
    return _get_stats(db, actor=_actor_of(current_user))


# ──────────────────────────────────────────────
# Memory File Upload Endpoints
# ──────────────────────────────────────────────

@router.post("/memory/parse-md")
async def parse_memory_md_file(
    file: UploadFile = File(...),
    agent_key: Optional[str] = Form(None),
    use_ai: bool = Form(False),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Parse a Markdown file into structured memory entries (preview only, no save).
    Supports structured format (## FACT / ## LEARNING headings) or AI-powered freeform parsing.
    """
    import os
    from app.services.langchain.memory_service import parse_md_to_memories, ai_parse_md_to_memories

    # Validate file type
    if not file.filename:
        raise HTTPException(status_code=400, detail="No file provided")
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in (".md", ".txt"):
        raise HTTPException(status_code=400, detail="Only .md and .txt files are supported")

    # Read and validate size
    content_bytes = await file.read()
    if len(content_bytes) > 500_000:  # 500KB limit
        raise HTTPException(status_code=413, detail="File too large. Maximum size is 500KB.")
    if len(content_bytes) == 0:
        raise HTTPException(status_code=400, detail="File is empty")

    md_content = content_bytes.decode("utf-8", errors="replace")

    try:
        if use_ai:
            entries = await ai_parse_md_to_memories(md_content, db)
        else:
            entries = parse_md_to_memories(md_content)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Parsing failed: {str(e)}")

    if not entries:
        raise HTTPException(status_code=422, detail="No memory entries could be parsed from the file. Check the format.")

    return {"entries": entries, "count": len(entries), "file_name": file.filename}


@router.post("/memory/upload-md")
def commit_memory_upload(
    body: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Commit parsed memory entries to the database (called after preview/parse-md)."""
    from app.services.langchain.memory_service import bulk_store_memories

    entries = body.get("entries", [])
    if not entries:
        raise HTTPException(status_code=400, detail="No entries to save")

    agent_key = body.get("agent_key") or None
    created_ids = bulk_store_memories(db, entries, agent_key=agent_key, created_by=current_user.id)
    return {"created_ids": created_ids, "count": len(created_ids)}


# ──────────────────────────────────────────────
# MCP Server Management Endpoints
# ──────────────────────────────────────────────

@router.get("/mcp/servers")
def list_mcp_servers(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List all configured MCP servers."""
    from app.models.mcp_config import MCPServerConfig

    servers = db.query(MCPServerConfig).order_by(MCPServerConfig.created_at.desc()).all()
    return [
        {
            "id": s.id,
            "server_name": s.server_name,
            "display_name": s.display_name,
            "description": s.description,
            "transport_type": s.transport_type,
            "command": s.command,
            "url": s.url,
            "is_enabled": s.is_enabled,
            "created_at": s.created_at,
        }
        for s in servers
    ]


@router.post("/mcp/servers")
def add_mcp_server(
    body: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Add a new MCP server configuration."""
    from app.models.mcp_config import MCPServerConfig

    server_name = body.get("server_name", "")
    if not server_name.strip():
        raise HTTPException(status_code=400, detail="server_name is required")

    existing = db.query(MCPServerConfig).filter(MCPServerConfig.server_name == server_name).first()
    if existing:
        raise HTTPException(status_code=409, detail=f"Server '{server_name}' already exists")

    config = MCPServerConfig(
        server_name=server_name,
        display_name=body.get("display_name", server_name),
        description=body.get("description"),
        transport_type=body.get("transport_type", "stdio"),
        command=body.get("command"),
        args=body.get("args", []),
        url=body.get("url"),
        env_vars=body.get("env_vars", {}),
        is_enabled=body.get("is_enabled", True),
        created_by=current_user.id,
    )
    db.add(config)
    db.commit()
    db.refresh(config)

    return {"id": config.id, "server_name": config.server_name, "status": "created"}


@router.put("/mcp/servers/{server_id}")
def update_mcp_server(
    server_id: int,
    body: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Update an MCP server configuration."""
    from app.models.mcp_config import MCPServerConfig

    config = db.query(MCPServerConfig).filter(MCPServerConfig.id == server_id).first()
    if not config:
        raise HTTPException(status_code=404, detail="MCP server not found")

    updatable = ["display_name", "description", "transport_type", "command", "args", "url", "env_vars", "is_enabled"]
    for field in updatable:
        if field in body:
            setattr(config, field, body[field])

    db.commit()
    return {"id": config.id, "status": "updated"}


@router.delete("/mcp/servers/{server_id}")
def delete_mcp_server(
    server_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delete an MCP server configuration."""
    from app.models.mcp_config import MCPServerConfig

    config = db.query(MCPServerConfig).filter(MCPServerConfig.id == server_id).first()
    if not config:
        raise HTTPException(status_code=404, detail="MCP server not found")

    db.delete(config)
    db.commit()
    return {"status": "deleted"}


@router.post("/mcp/servers/{server_id}/test")
async def test_mcp_server(
    server_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Test connection to an MCP server."""
    from app.models.mcp_config import MCPServerConfig
    from app.services.mcp.mcp_client import MCPClientManager

    config = db.query(MCPServerConfig).filter(MCPServerConfig.id == server_id).first()
    if not config:
        raise HTTPException(status_code=404, detail="MCP server not found")

    client = MCPClientManager(db)
    result = await client.test_connection(config.server_name)
    return result


@router.get("/mcp/servers/{server_id}/tools")
async def get_mcp_server_tools(
    server_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List tools available from an MCP server."""
    from app.models.mcp_config import MCPServerConfig
    from app.services.mcp.mcp_client import MCPClientManager

    config = db.query(MCPServerConfig).filter(MCPServerConfig.id == server_id).first()
    if not config:
        raise HTTPException(status_code=404, detail="MCP server not found")

    client = MCPClientManager(db)
    connected = await client.connect(config.server_name)
    if not connected:
        raise HTTPException(status_code=502, detail="Could not connect to MCP server")

    tools = await client.list_tools(config.server_name)
    await client.disconnect(config.server_name)
    return {"server_name": config.server_name, "tools": tools}
