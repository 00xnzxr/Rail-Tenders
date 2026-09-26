"""
DRPL - OpenAI Agents SDK Tool Bridge
Converts DRPL LangChain BaseTool instances into OpenAI Agents SDK function tools.
"""

import inspect
import json
import logging

from langchain_core.tools import BaseTool

logger = logging.getLogger(__name__)


def bridge_drpl_tools(drpl_tools: list[BaseTool]) -> list:
    """
    Convert a list of DRPL LangChain BaseTool instances into
    OpenAI Agents SDK FunctionTool instances.

    Each LangChain tool's args_schema (Pydantic model) is preserved so the
    SDK emits the correct JSON schema to the model.  The wrapper calls
    the tool's async `_arun()` or sync `_run()` method at runtime.
    """
    from agents import FunctionTool

    bridged: list = []

    for tool in drpl_tools:
        # Build a JSON-schema dict from the tool's args_schema
        if hasattr(tool, "args_schema") and tool.args_schema is not None:
            params_schema = tool.args_schema.model_json_schema()
        else:
            # Fallback: single-string input
            params_schema = {
                "type": "object",
                "properties": {
                    "input": {
                        "type": "string",
                        "description": "Input for the tool",
                    }
                },
                "required": ["input"],
            }

        # Build wrapper — capture tool via default arg to avoid late-binding closure bug
        async def _wrapper(ctx, args_json: str, _t=tool) -> str:
            try:
                args = json.loads(args_json)
            except (ValueError, TypeError):
                args = {"input": args_json}

            # Detect single-property schemas and flatten to a single value
            schema_props = {}
            if hasattr(_t, "args_schema") and _t.args_schema is not None:
                schema_props = _t.args_schema.model_json_schema().get("properties", {})

            if len(schema_props) == 1:
                key = next(iter(schema_props))
                tool_input = args.get(key, args_json)
            else:
                tool_input = args

            try:
                if hasattr(_t, "_arun") and inspect.iscoroutinefunction(_t._arun):
                    if isinstance(tool_input, dict):
                        result = await _t._arun(**tool_input)
                    else:
                        result = await _t._arun(tool_input)
                else:
                    if isinstance(tool_input, dict):
                        result = _t._run(**tool_input)
                    else:
                        result = _t._run(tool_input)
                return str(result)
            except Exception as e:
                logger.error(f"Tool '{_t.name}' execution error: {e}")
                return json.dumps({"error": f"Tool '{_t.name}' failed: {e}"})

        fn_tool = FunctionTool(
            name=tool.name,
            description=tool.description or f"Tool: {tool.name}",
            params_json_schema=params_schema,
            on_invoke_tool=_wrapper,
        )
        bridged.append(fn_tool)
        logger.debug(f"Bridged DRPL tool '{tool.name}' -> OpenAI Agents SDK FunctionTool")

    return bridged
