"""
DRPL - OpenAI Agents SDK Guardrails
Input and output guardrails for agent safety and validation.
Supports both standalone validation and SDK-integrated guardrails.
"""

import json
import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

# Configurable limits
DEFAULT_MAX_INPUT_CHARS = 500_000  # ~125k tokens
DEFAULT_MAX_OUTPUT_CHARS = 200_000  # ~50k tokens

# Prompt injection detection patterns (case-insensitive)
INJECTION_PATTERNS = [
    # Direct instruction override
    r"ignore\s+(all\s+)?previous\s+instructions",
    r"disregard\s+(all\s+)?(your\s+)?instructions",
    r"forget\s+(all\s+)?(your\s+)?(system\s+)?prompt",
    r"override\s+(your\s+)?instructions",
    r"new\s+instructions?\s*:",
    r"system\s*:\s*you\s+are\s+now",
    # Role impersonation
    r"you\s+are\s+now\s+(a\s+)?different",
    r"pretend\s+(to\s+be|you\s+are)",
    r"act\s+as\s+if\s+your\s+instructions",
    # Jailbreak patterns
    r"do\s+anything\s+now",
    r"developer\s+mode\s+(enabled|on|activated)",
    r"ignore\s+safety\s+(guidelines|rules|filters)",
    r"bypass\s+(content\s+)?(filter|policy|safety)",
]

# Pre-compile patterns for performance
_COMPILED_PATTERNS = [re.compile(p, re.IGNORECASE) for p in INJECTION_PATTERNS]


async def check_input_guardrail(
    input_text: str,
    max_chars: int = DEFAULT_MAX_INPUT_CHARS,
) -> dict:
    """
    Check input against safety rules before agent execution.

    Returns:
        Dict with "passed" (bool) and "reason" (str if blocked)
    """
    if not input_text or not input_text.strip():
        return {"passed": False, "reason": "Empty input"}

    # Check input length limit
    if len(input_text) > max_chars:
        return {
            "passed": False,
            "reason": f"Input exceeds maximum length ({len(input_text):,} chars, max {max_chars:,})",
        }

    # Check for prompt injection patterns
    for pattern in _COMPILED_PATTERNS:
        match = pattern.search(input_text)
        if match:
            logger.warning(f"Potential prompt injection detected: '{match.group()}'")
            return {
                "passed": False,
                "reason": "Input flagged by safety guardrail. Please rephrase your request.",
            }

    return {"passed": True, "reason": None}


async def check_output_guardrail(
    output_text: str,
    output_schema: Optional[dict] = None,
    max_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
) -> dict:
    """
    Validate agent output against safety and schema rules.

    Args:
        output_text: The agent's output text
        output_schema: Optional JSON schema to validate against
        max_chars: Maximum output length

    Returns:
        Dict with "passed" (bool) and "reason" (str if failed)
    """
    if not output_text:
        return {"passed": True, "reason": None}

    # Output length check
    if len(output_text) > max_chars:
        return {
            "passed": False,
            "reason": f"Output exceeds maximum length ({len(output_text):,} chars, max {max_chars:,})",
        }

    # Schema validation if output_schema is provided
    if output_schema:
        try:
            parsed = json.loads(output_text)
            # Required-field validation
            required = output_schema.get("required", [])
            missing = [f for f in required if f not in parsed]
            if missing:
                return {
                    "passed": False,
                    "reason": f"Output missing required fields: {missing}",
                }

            # Type validation for top-level properties
            properties = output_schema.get("properties", {})
            type_errors = []
            type_map = {
                "string": str, "integer": int, "number": (int, float),
                "boolean": bool, "array": list, "object": dict,
            }
            for field_name, field_def in properties.items():
                if field_name in parsed:
                    expected_type = type_map.get(field_def.get("type", ""))
                    if expected_type and not isinstance(parsed[field_name], expected_type):
                        type_errors.append(
                            f"'{field_name}' expected {field_def.get('type')} "
                            f"but got {type(parsed[field_name]).__name__}"
                        )
            if type_errors:
                return {
                    "passed": False,
                    "reason": f"Output type errors: {'; '.join(type_errors)}",
                }

        except json.JSONDecodeError:
            return {
                "passed": False,
                "reason": "Output is not valid JSON but schema requires structured output",
            }

    return {"passed": True, "reason": None}


def build_guardrails_for_agent(agent) -> dict:
    """
    Build guardrail configuration from a CustomAgent's settings.

    Returns dict with "input_guardrails" and "output_guardrails" lists
    suitable for passing to the OpenAI Agents SDK Agent constructor.
    """
    from agents import InputGuardrail, OutputGuardrail, GuardrailFunctionOutput

    input_guardrails = []
    output_guardrails = []

    # Always add basic input safety guardrail
    async def _input_safety(ctx, agent_obj, input_data):
        text = str(input_data) if not isinstance(input_data, str) else input_data
        result = await check_input_guardrail(text)
        return GuardrailFunctionOutput(
            output_info=result,
            tripwire_triggered=not result["passed"],
        )

    input_guardrails.append(
        InputGuardrail(guardrail_function=_input_safety)
    )

    # Add output schema guardrail if the agent has an output_schema
    if hasattr(agent, "output_schema") and agent.output_schema:
        schema = agent.output_schema

        async def _output_schema_check(ctx, agent_obj, output):
            text = str(output) if not isinstance(output, str) else output
            result = await check_output_guardrail(text, schema)
            return GuardrailFunctionOutput(
                output_info=result,
                tripwire_triggered=not result["passed"],
            )

        output_guardrails.append(
            OutputGuardrail(guardrail_function=_output_schema_check)
        )

    return {
        "input_guardrails": input_guardrails,
        "output_guardrails": output_guardrails,
    }
