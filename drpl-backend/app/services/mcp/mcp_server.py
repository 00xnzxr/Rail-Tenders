"""
DRPL MCP Server
Exposes the DRPL platform's capabilities as MCP (Model Context Protocol) tools
that external clients (Claude Desktop, other LangChain agents) can call.

Runs as an optional SSE server alongside the main FastAPI application.
"""

import json
import logging
import asyncio
from typing import Any

from mcp.server import Server
from mcp.types import (
    Tool,
    TextContent,
    Resource,
    ResourceTemplate,
)

logger = logging.getLogger(__name__)

# Create MCP server instance
server = Server("drpl-tender-intelligence")


def _get_db():
    """Get a database session for MCP handlers."""
    from app.core.database import SessionLocal
    return SessionLocal()


# --- MCP Resources ---

@server.list_resources()
async def list_resources() -> list[Resource]:
    """List available DRPL resources."""
    return []


@server.list_resource_templates()
async def list_resource_templates() -> list[ResourceTemplate]:
    """List resource templates (parameterized resources)."""
    return [
        ResourceTemplate(
            uriTemplate="drpl://tenders/{tender_id}",
            name="Tender Details",
            description="Get full details for a specific tender by ID",
        ),
        ResourceTemplate(
            uriTemplate="drpl://checklist/{tender_id}",
            name="Tender Checklist",
            description="Get document checklist for a specific tender",
        ),
        ResourceTemplate(
            uriTemplate="drpl://analysis/{tender_id}",
            name="Tender Analysis",
            description="Get AI analysis results for a specific tender",
        ),
    ]


@server.read_resource()
async def read_resource(uri: str) -> str:
    """Read a DRPL resource by URI."""
    db = _get_db()
    try:
        if uri.startswith("drpl://tenders/"):
            tender_id = int(uri.split("/")[-1])
            from app.models.tender import Tender
            tender = db.query(Tender).filter(Tender.id == tender_id).first()
            if not tender:
                return f"Tender {tender_id} not found"
            return json.dumps({
                "id": tender.id, "title": tender.title, "portal": tender.portal,
                "department": tender.department, "description": tender.description,
                "estimated_value": tender.estimated_value, "status": tender.status,
                "ai_category": tender.ai_category, "ai_summary": tender.ai_summary,
            }, default=str)

        if uri.startswith("drpl://checklist/"):
            tender_id = int(uri.split("/")[-1])
            from app.models.checklist import ChecklistItem
            items = db.query(ChecklistItem).filter(ChecklistItem.tender_id == tender_id).all()
            return json.dumps([{
                "name": i.item_name, "required": i.is_required, "uploaded": i.is_uploaded,
            } for i in items])

        if uri.startswith("drpl://analysis/"):
            tender_id = int(uri.split("/")[-1])
            from app.models.document_analysis import TenderAnalysisSummary
            summary = db.query(TenderAnalysisSummary).filter(
                TenderAnalysisSummary.tender_id == tender_id
            ).first()
            if not summary:
                return f"No analysis found for tender {tender_id}"
            return json.dumps({
                "total_requirements": summary.total_requirements,
                "total_critical_flags": summary.total_critical_flags,
                "completeness_score": summary.completeness_score,
                "requirement_summary": summary.requirement_summary,
            }, default=str)

        return f"Unknown resource: {uri}"
    finally:
        db.close()


# --- MCP Tools ---

@server.list_tools()
async def list_tools() -> list[Tool]:
    """List available MCP tools."""
    return [
        Tool(
            name="search_tenders",
            description="Search the DRPL tender database by keyword, portal, or department",
            inputSchema={
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "Search keyword"},
                    "portal": {"type": "string", "description": "Portal filter (GeM, IREPS, CPPP)"},
                    "max_results": {"type": "integer", "default": 10},
                },
            },
        ),
        Tool(
            name="analyze_tender",
            description="Run AI-powered deep analysis on a tender",
            inputSchema={
                "type": "object",
                "properties": {
                    "tender_id": {"type": "integer", "description": "Tender ID to analyze"},
                },
                "required": ["tender_id"],
            },
        ),
        Tool(
            name="generate_document",
            description="Generate a professional document using AI",
            inputSchema={
                "type": "object",
                "properties": {
                    "document_type": {"type": "string", "enum": ["proposal", "cost_statement", "letter", "certificate", "custom"]},
                    "prompt": {"type": "string", "description": "Instructions for document content"},
                    "title": {"type": "string", "default": "Generated Document"},
                },
                "required": ["prompt"],
            },
        ),
        Tool(
            name="web_search",
            description="Search the web for tender, market rate, or technical information",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "max_results": {"type": "integer", "default": 5},
                    "domains": {"type": "array", "items": {"type": "string"}, "description": "Restrict to domains"},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="store_memory",
            description="Store a fact or learning in long-term agent memory",
            inputSchema={
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "What to remember"},
                    "memory_type": {"type": "string", "enum": ["fact", "preference", "learning", "decision"]},
                    "keywords": {"type": "array", "items": {"type": "string"}},
                    "importance": {"type": "number", "default": 0.5},
                },
                "required": ["content"],
            },
        ),
        Tool(
            name="retrieve_memory",
            description="Search long-term agent memory for relevant information",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "top_k": {"type": "integer", "default": 5},
                },
                "required": ["query"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    """Handle MCP tool calls by delegating to DRPL services."""
    db = _get_db()
    try:
        result = await _dispatch_tool(name, arguments, db)
        return [TextContent(type="text", text=result)]
    except Exception as e:
        logger.error(f"MCP tool '{name}' failed: {e}")
        return [TextContent(type="text", text=f"Error: {str(e)}")]
    finally:
        db.close()


async def _dispatch_tool(name: str, args: dict, db) -> str:
    """Route MCP tool calls to the appropriate DRPL service."""

    if name == "search_tenders":
        from app.services.langchain.tools.tender_lookup_tool import TenderLookupTool
        tool = TenderLookupTool(db=db)
        return tool._run(**args)

    elif name == "analyze_tender":
        from app.services.langchain.graphs.document_analysis_agent import run_document_analysis
        result = await run_document_analysis(db, args["tender_id"])
        return json.dumps(result, default=str)

    elif name == "generate_document":
        from app.services.langchain.tools.document_generator_tool import DocumentGeneratorTool
        tool = DocumentGeneratorTool(db=db)
        return tool._run(**args)

    elif name == "web_search":
        from app.services.langchain.tools.web_search_tool import WebSearchTool
        tool = WebSearchTool()
        return tool._run(**args)

    elif name == "store_memory":
        from app.services.langchain.memory_service import store_memory
        mem = store_memory(db, agent_key="mcp_client", **args)
        return json.dumps({"stored": True, "memory_id": mem.id})

    elif name == "retrieve_memory":
        from app.services.langchain.memory_service import retrieve_memories
        memories = retrieve_memories(db, agent_key=None, **args)
        return json.dumps([{"content": m.content, "type": m.memory_type, "keywords": m.keywords} for m in memories], default=str)

    else:
        return f"Unknown tool: {name}"


# --- Server Runner ---

async def run_mcp_server_stdio():
    """Run the MCP server using stdio transport (for Claude Desktop)."""
    from mcp.server.stdio import stdio_server

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


async def run_mcp_server_sse(host: str = "0.0.0.0", port: int = 8001):
    """Run the MCP server using SSE transport (for web clients)."""
    from mcp.server.sse import SseServerTransport
    from starlette.applications import Starlette
    from starlette.routing import Route, Mount
    import uvicorn

    sse = SseServerTransport("/messages/")

    async def handle_sse(request):
        async with sse.connect_sse(request.scope, request.receive, request._send) as streams:
            await server.run(streams[0], streams[1], server.create_initialization_options())

    app = Starlette(
        routes=[
            Route("/sse", endpoint=handle_sse),
            Mount("/messages/", app=sse.handle_post_message),
        ]
    )

    config = uvicorn.Config(app, host=host, port=port, log_level="info")
    server_instance = uvicorn.Server(config)
    await server_instance.serve()
