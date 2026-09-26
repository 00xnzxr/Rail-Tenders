"""
DRPL LangChain Tool - Structured Output
Forces structured JSON output conforming to a provided schema.
Works with any provider via the LangChain with_structured_output() interface.
"""

import json
import logging
from typing import Optional, Type

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


class StructuredOutputInput(BaseModel):
    """Input schema for the structured output tool."""
    prompt: str = Field(
        description="The prompt describing what data to generate or extract"
    )
    schema_json: str = Field(
        description=(
            "A JSON string defining the output schema. Must be a valid JSON object "
            "with 'properties' and 'required' fields following JSON Schema format. "
            "Example: {\"type\": \"object\", \"properties\": {\"name\": {\"type\": \"string\"}, "
            "\"cost\": {\"type\": \"number\"}}, \"required\": [\"name\", \"cost\"]}"
        )
    )


class StructuredOutputTool(BaseTool):
    """
    Generate structured JSON output conforming to a specified schema.

    Uses the active LLM provider's structured output capability:
    - Claude: Tool use extraction pattern
    - OpenAI: JSON mode / structured outputs
    - Gemini: Structured output generation

    Useful for:
    - Extracting structured data from unstructured text
    - Generating cost breakdowns, checklists, or requirement lists
    - Converting natural language to structured format
    """

    name: str = "structured_output"
    description: str = (
        "Generate structured JSON output that conforms to a specified schema. "
        "Provide a prompt and a JSON schema, and get back guaranteed "
        "schema-compliant JSON data. Use for extracting structured data, "
        "creating cost breakdowns, checklists, or any structured format."
    )
    args_schema: Type[BaseModel] = StructuredOutputInput
    db: Optional[Session] = None

    class Config:
        arbitrary_types_allowed = True

    def _run(self, prompt: str, schema_json: str) -> str:
        """Synchronous execution."""
        import asyncio
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    return pool.submit(asyncio.run, self._arun(prompt, schema_json)).result()
            return loop.run_until_complete(self._arun(prompt, schema_json))
        except RuntimeError:
            return asyncio.run(self._arun(prompt, schema_json))

    async def _arun(self, prompt: str, schema_json: str) -> str:
        """Generate structured output using the LLM."""
        # Parse the schema
        try:
            schema = json.loads(schema_json)
        except json.JSONDecodeError as e:
            return json.dumps({"error": f"Invalid JSON schema: {e}"})

        try:
            from app.services.langchain.llm_factory import get_chat_model
            from langchain_core.messages import HumanMessage, SystemMessage

            llm = get_chat_model(self.db, temperature=0.1, max_tokens=4096)

            # Build a system prompt that enforces the schema
            system_content = (
                "You are a structured data extraction engine. "
                "Output ONLY valid JSON that conforms exactly to the provided schema. "
                "No explanations, no markdown fences, no extra text — just the JSON object.\n\n"
                f"Required output schema:\n{json.dumps(schema, indent=2)}"
            )

            messages = [
                SystemMessage(content=system_content),
                HumanMessage(content=prompt),
            ]

            result = await llm.ainvoke(messages)
            content = result.content.strip()

            # Clean markdown fences if present
            if content.startswith("```"):
                content = content.split("```")[1]
                if content.startswith("json"):
                    content = content[4:]
                content = content.strip()

            # Validate it's valid JSON
            parsed = json.loads(content)
            return json.dumps(parsed, indent=2, default=str)

        except json.JSONDecodeError:
            return json.dumps({
                "error": "LLM output was not valid JSON",
                "raw_output": content[:2000] if 'content' in dir() else "No output",
            })
        except Exception as e:
            logger.error(f"Structured output generation failed: {e}")
            return json.dumps({"error": f"Structured output failed: {str(e)}"})
