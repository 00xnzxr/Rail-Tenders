"""
DRPL - OpenAI Responses API Client
Direct httpx client for the Responses API (/v1/responses).
Used by the direct API path (ai_service.py) as a modern replacement
for Chat Completions with server-side conversation state.
"""

import json
import logging
import time
from typing import Any, Optional

import httpx
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

RESPONSES_API_URL = "https://api.openai.com/v1/responses"


async def call_openai_responses(
    system_prompt: str,
    user_prompt: str,
    api_key: str,
    model: str = "gpt-4o-mini",
    temperature: float = 0.7,
    max_tokens: int = 4096,
    tools: Optional[list[dict]] = None,
    previous_response_id: Optional[str] = None,
    response_format: Optional[dict] = None,
    store: bool = False,
) -> dict:
    """
    Call the OpenAI Responses API.

    Key advantages over Chat Completions:
    - previous_response_id for server-side conversation chaining
    - Built-in hosted tools (web_search, code_interpreter)
    - response_format with json_schema for guaranteed structured output
    - Strict function calling by default

    Args:
        system_prompt: System instructions
        user_prompt: User message
        api_key: OpenAI API key
        model: Model name (e.g. "gpt-4o", "gpt-4o-mini")
        temperature: Sampling temperature
        max_tokens: Max output tokens
        tools: Optional tool definitions (OpenAI format)
        previous_response_id: Chain to a previous response for server-side state
        response_format: Structured output format (e.g. {"type": "json_schema", ...})
        store: Whether OpenAI keeps the response for later chaining. Default
            False: no caller chains, and a stored response leaves the tender
            text in the org's response store. Pass True only alongside a
            ``previous_response_id`` flow that needs it.

    Returns:
        Dict with keys: text, response_id, usage, tool_calls
    """
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    # Build input
    input_items = []

    if previous_response_id:
        # When chaining, only send the new user message — server has history
        input_items.append({
            "type": "message",
            "role": "user",
            "content": user_prompt,
        })
    else:
        # First turn — include system instructions
        input_items.append({
            "type": "message",
            "role": "developer",
            "content": system_prompt,
        })
        input_items.append({
            "type": "message",
            "role": "user",
            "content": user_prompt,
        })

    request_body: dict[str, Any] = {
        "model": model,
        "input": input_items,
        "store": store,
    }

    # Temperature
    if temperature is not None:
        request_body["temperature"] = temperature

    # Max tokens
    if max_tokens:
        request_body["max_output_tokens"] = max_tokens

    # Previous response for server-side conversation chaining
    if previous_response_id:
        request_body["previous_response_id"] = previous_response_id

    # Tools
    if tools:
        request_body["tools"] = tools

    # Structured output format
    if response_format:
        request_body["text"] = {"format": response_format}

    start_time = time.time()

    async with httpx.AsyncClient(timeout=180.0) as client:
        response = await client.post(
            RESPONSES_API_URL,
            headers=headers,
            json=request_body,
        )

        elapsed_ms = int((time.time() - start_time) * 1000)

        if response.status_code == 429:
            raise Exception("OpenAI Responses API rate limit hit.")

        if response.status_code != 200:
            error_detail = f"HTTP {response.status_code}"
            try:
                error_data = response.json()
                error_msg = error_data.get("error", {}).get("message", "")
                if error_msg:
                    error_detail = error_msg
            except Exception:
                pass
            raise Exception(f"OpenAI Responses API error: {error_detail}")

        data = response.json()

    # Extract text output from response
    text_output = ""
    tool_calls = []

    for item in data.get("output", []):
        if item.get("type") == "message":
            for content_block in item.get("content", []):
                if content_block.get("type") == "output_text":
                    text_output += content_block.get("text", "")
        elif item.get("type") == "function_call":
            tool_calls.append({
                "id": item.get("call_id"),
                "name": item.get("name"),
                "arguments": item.get("arguments"),
            })

    # Extract usage
    usage_data = data.get("usage", {})
    usage = {
        "input_tokens": usage_data.get("input_tokens", 0),
        "output_tokens": usage_data.get("output_tokens", 0),
        "total_tokens": usage_data.get("total_tokens", 0),
    }

    return {
        "text": text_output.strip(),
        "response_id": data.get("id"),
        "usage": usage,
        "tool_calls": tool_calls,
        "elapsed_ms": elapsed_ms,
        "model": data.get("model", model),
    }


def extract_response_id_from_metadata(metadata_json: Optional[dict]) -> Optional[str]:
    """
    Extract the OpenAI response_id from conversation history metadata.
    Used to chain responses via previous_response_id.
    """
    if not metadata_json:
        return None
    return metadata_json.get("openai_response_id")
