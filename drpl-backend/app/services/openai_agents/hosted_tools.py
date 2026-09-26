"""
DRPL - OpenAI Hosted Tools Adapter
Wraps OpenAI's built-in hosted tools (web search, code interpreter, file search)
for use in the DRPL agent system.
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Keys that map to OpenAI hosted tools (stored in agent_tools registry)
HOSTED_TOOL_KEYS = {
    "openai_web_search",
    "openai_code_interpreter",
    "openai_file_search",
}


def get_hosted_tools(
    tool_keys: list[str],
    vector_store_ids: Optional[list[str]] = None,
) -> list:
    """
    Get OpenAI hosted tool instances for the given tool keys.

    These tools run server-side on OpenAI's infrastructure — no local
    execution, no extra API keys beyond the OpenAI key.

    Args:
        tool_keys: List of tool_key strings (e.g. ["openai_web_search"])
        vector_store_ids: Required for file_search — OpenAI vector store IDs

    Returns:
        List of OpenAI hosted tool instances
    """
    from agents import WebSearchTool, CodeInterpreterTool, FileSearchTool

    tools = []

    for key in tool_keys:
        if key == "openai_web_search":
            tools.append(WebSearchTool())
            logger.debug("Added OpenAI hosted WebSearchTool")

        elif key == "openai_code_interpreter":
            tools.append(CodeInterpreterTool())
            logger.debug("Added OpenAI hosted CodeInterpreterTool")

        elif key == "openai_file_search":
            if vector_store_ids:
                tools.append(FileSearchTool(vector_store_ids=vector_store_ids))
                logger.debug(f"Added OpenAI hosted FileSearchTool with {len(vector_store_ids)} vector stores")
            else:
                logger.warning("openai_file_search requires vector_store_ids — skipping")

    return tools


def is_hosted_tool(tool_key: str) -> bool:
    """Check if a tool_key refers to an OpenAI hosted tool."""
    return tool_key in HOSTED_TOOL_KEYS
