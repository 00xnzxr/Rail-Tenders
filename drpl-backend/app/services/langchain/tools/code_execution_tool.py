"""
DRPL LangChain Tool - Code Execution
Executes Python code for calculations, data processing, and verification
using Google Gemini's sandboxed code execution capability.
"""

import json
import logging
from typing import Optional, Type

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


class CodeExecutionInput(BaseModel):
    """Input schema for the code execution tool."""
    request: str = Field(
        description=(
            "A natural language description of what code to execute. "
            "Examples: 'Calculate the total cost of 500 units at Rs 1,234 each with 18% GST', "
            "'Convert this CSV data to a summary table', "
            "'Verify that 15% of 4,50,000 equals 67,500'"
        )
    )


class GeminiCodeExecutionTool(BaseTool):
    """
    Execute Python code via Google Gemini's sandboxed code execution.

    This tool sends a natural language request to Gemini with code execution
    enabled. Gemini writes and runs Python code in a sandbox, returning
    both the code and its output. Useful for:
    - Mathematical calculations and verification
    - Data transformations and formatting
    - Cost breakdowns and financial calculations
    - Unit conversions and engineering calculations
    """

    name: str = "code_execution"
    description: str = (
        "Execute Python code for calculations, data processing, or verification. "
        "Send a natural language description of what to calculate or process. "
        "The code runs in a secure sandbox — use for math, cost calculations, "
        "data formatting, and verification tasks."
    )
    args_schema: Type[BaseModel] = CodeExecutionInput
    db: Optional[Session] = None

    class Config:
        arbitrary_types_allowed = True

    def _get_api_key(self) -> Optional[str]:
        """Resolve Google API key."""
        from app.services.langchain.provider_config import get_api_key
        return get_api_key(self.db, "google")

    def _run(self, request: str) -> str:
        """Synchronous execution (uses async internally)."""
        import asyncio
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # We're in an async context — run in a thread
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    return pool.submit(asyncio.run, self._arun(request)).result()
            return loop.run_until_complete(self._arun(request))
        except RuntimeError:
            return asyncio.run(self._arun(request))

    async def _arun(self, request: str) -> str:
        """Execute code via Gemini's code execution capability."""
        api_key = self._get_api_key()
        if not api_key:
            return json.dumps({
                "error": "Google API key not configured. Set it in Platform Settings.",
                "result": None,
            })

        try:
            from google import genai
            from google.genai import types

            client = genai.Client(api_key=api_key)

            # Enable code execution tool
            code_execution_tool = types.Tool(
                code_execution=types.ToolCodeExecution()
            )

            config = types.GenerateContentConfig(
                tools=[code_execution_tool],
                temperature=0.1,  # Low temperature for precise calculations
                max_output_tokens=4096,
            )

            response = client.models.generate_content(
                model=settings.gemini_search_model or "gemini-2.5-flash",
                contents=f"Execute Python code to accomplish this task:\n\n{request}",
                config=config,
            )

            # Extract results from response parts
            result_parts = []
            code_blocks = []

            if response.candidates:
                for part in response.candidates[0].content.parts:
                    if hasattr(part, "text") and part.text:
                        result_parts.append(part.text)
                    if hasattr(part, "executable_code") and part.executable_code:
                        code_blocks.append({
                            "language": getattr(part.executable_code, "language", "python"),
                            "code": part.executable_code.code,
                        })
                    if hasattr(part, "code_execution_result") and part.code_execution_result:
                        result_parts.append(
                            f"**Code Output:**\n{part.code_execution_result.output}"
                        )

            output = {
                "result": "\n".join(result_parts) if result_parts else "No output produced",
                "code_executed": code_blocks,
            }

            return json.dumps(output, indent=2, default=str)

        except Exception as e:
            logger.error(f"Code execution failed: {e}")
            return json.dumps({
                "error": f"Code execution failed: {str(e)}",
                "result": None,
            })
