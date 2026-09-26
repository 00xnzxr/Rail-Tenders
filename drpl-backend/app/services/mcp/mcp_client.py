"""
DRPL MCP Client
Manages connections to external MCP servers and wraps their tools
as LangChain BaseTool instances for agent use.
"""

import json
import logging
from typing import Optional, Any

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.models.mcp_config import MCPServerConfig

logger = logging.getLogger(__name__)


class MCPClientManager:
    """
    Manages connections to external MCP servers configured in the database.
    Provides tool discovery and invocation capabilities.
    """

    def __init__(self, db: Session):
        self.db = db
        self._sessions: dict[str, Any] = {}

    async def connect(self, server_name: str) -> bool:
        """Connect to an MCP server by name."""
        config = self.db.query(MCPServerConfig).filter(
            MCPServerConfig.server_name == server_name,
            MCPServerConfig.is_enabled == True,
        ).first()

        if not config:
            logger.error(f"MCP server config not found or disabled: {server_name}")
            return False

        try:
            from mcp import ClientSession

            if config.transport_type == "stdio":
                from mcp.client.stdio import stdio_client, StdioServerParameters

                params = StdioServerParameters(
                    command=config.command,
                    args=config.args or [],
                    env=config.env_vars or {},
                )
                # Create and store the session
                transport = stdio_client(params)
                read, write = await transport.__aenter__()
                session = ClientSession(read, write)
                await session.__aenter__()
                await session.initialize()
                self._sessions[server_name] = {
                    "session": session,
                    "transport": transport,
                    "config": config,
                }
                logger.info(f"Connected to MCP server: {server_name} (stdio)")
                return True

            elif config.transport_type == "sse":
                from mcp.client.sse import sse_client

                transport = sse_client(url=config.url)
                read, write = await transport.__aenter__()
                session = ClientSession(read, write)
                await session.__aenter__()
                await session.initialize()
                self._sessions[server_name] = {
                    "session": session,
                    "transport": transport,
                    "config": config,
                }
                logger.info(f"Connected to MCP server: {server_name} (sse)")
                return True

            else:
                logger.error(f"Unsupported transport type: {config.transport_type}")
                return False

        except Exception as e:
            logger.error(f"Failed to connect to MCP server '{server_name}': {e}")
            return False

    async def disconnect(self, server_name: str):
        """Disconnect from an MCP server."""
        if server_name in self._sessions:
            session_data = self._sessions.pop(server_name)
            try:
                await session_data["session"].__aexit__(None, None, None)
                await session_data["transport"].__aexit__(None, None, None)
            except Exception as e:
                logger.warning(f"Error disconnecting from {server_name}: {e}")

    async def list_tools(self, server_name: str) -> list[dict]:
        """List tools available from a connected MCP server."""
        if server_name not in self._sessions:
            if not await self.connect(server_name):
                return []

        session = self._sessions[server_name]["session"]
        try:
            result = await session.list_tools()
            return [
                {
                    "name": tool.name,
                    "description": tool.description or "",
                    "input_schema": tool.inputSchema if hasattr(tool, 'inputSchema') else {},
                }
                for tool in result.tools
            ]
        except Exception as e:
            logger.error(f"Failed to list tools from {server_name}: {e}")
            return []

    async def call_tool(self, server_name: str, tool_name: str, arguments: dict) -> str:
        """Call a tool on a connected MCP server."""
        if server_name not in self._sessions:
            if not await self.connect(server_name):
                raise ConnectionError(f"Cannot connect to MCP server: {server_name}")

        session = self._sessions[server_name]["session"]
        result = await session.call_tool(tool_name, arguments)

        # Extract text content from result
        if result.content:
            texts = [c.text for c in result.content if hasattr(c, 'text')]
            return "\n".join(texts) if texts else str(result.content)
        return ""

    async def test_connection(self, server_name: str) -> dict:
        """Test connection to an MCP server and return available tools."""
        try:
            connected = await self.connect(server_name)
            if not connected:
                return {"status": "failed", "error": "Connection failed"}

            tools = await self.list_tools(server_name)
            await self.disconnect(server_name)

            return {
                "status": "connected",
                "tools_count": len(tools),
                "tools": tools,
            }
        except Exception as e:
            return {"status": "failed", "error": str(e)}

    def get_langchain_tools(self, server_name: str) -> list[BaseTool]:
        """
        Wrap all tools from a connected MCP server as LangChain BaseTool instances.
        These can be passed directly to LangChain agents.
        """
        # This is a synchronous convenience — tools are created as wrappers
        # that will call the MCP server asynchronously when invoked
        if server_name not in self._sessions:
            logger.warning(f"MCP server {server_name} not connected")
            return []

        session_data = self._sessions[server_name]
        # We need the tool list — this requires async, so we cache it
        # This method should be called after list_tools has been called
        return []  # Tools are loaded dynamically via load_mcp_tools_for_agent


def mcp_tool_to_langchain_tool(
    tool_info: dict,
    client_manager: MCPClientManager,
    server_name: str,
) -> BaseTool:
    """
    Wrap a single MCP tool as a LangChain BaseTool.

    Args:
        tool_info: Dict with name, description, input_schema from list_tools
        client_manager: MCPClientManager instance for making calls
        server_name: Name of the MCP server this tool belongs to

    Returns:
        A LangChain BaseTool that delegates calls to the MCP server
    """

    class MCPWrappedTool(BaseTool):
        name: str = tool_info["name"]
        description: str = tool_info.get("description", f"MCP tool: {tool_info['name']}")
        _client: MCPClientManager = client_manager
        _server: str = server_name

        class Config:
            arbitrary_types_allowed = True

        def _run(self, **kwargs) -> str:
            import asyncio
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    import concurrent.futures
                    with concurrent.futures.ThreadPoolExecutor() as pool:
                        return pool.submit(
                            lambda: asyncio.run(self._client.call_tool(self._server, self.name, kwargs))
                        ).result(timeout=60)
                else:
                    return asyncio.run(self._client.call_tool(self._server, self.name, kwargs))
            except Exception as e:
                return f"MCP tool error: {str(e)}"

        async def _arun(self, **kwargs) -> str:
            return await self._client.call_tool(self._server, self.name, kwargs)

    return MCPWrappedTool()


async def load_mcp_tools_for_agent(
    db: Session,
    server_names: Optional[list[str]] = None,
) -> list[BaseTool]:
    """
    Load all tools from configured MCP servers as LangChain tools.

    Args:
        db: Database session
        server_names: Optional list of specific servers (default: all enabled)

    Returns:
        List of LangChain BaseTool instances wrapping MCP tools
    """
    if server_names:
        configs = db.query(MCPServerConfig).filter(
            MCPServerConfig.server_name.in_(server_names),
            MCPServerConfig.is_enabled == True,
        ).all()
    else:
        configs = db.query(MCPServerConfig).filter(
            MCPServerConfig.is_enabled == True,
        ).all()

    if not configs:
        return []

    client = MCPClientManager(db)
    tools = []

    for config in configs:
        try:
            connected = await client.connect(config.server_name)
            if connected:
                mcp_tools = await client.list_tools(config.server_name)
                for tool_info in mcp_tools:
                    lc_tool = mcp_tool_to_langchain_tool(tool_info, client, config.server_name)
                    tools.append(lc_tool)
                logger.info(f"Loaded {len(mcp_tools)} tools from MCP server: {config.server_name}")
        except Exception as e:
            logger.error(f"Failed to load tools from MCP server '{config.server_name}': {e}")

    return tools
