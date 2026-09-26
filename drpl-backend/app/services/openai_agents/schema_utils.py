"""
DRPL - Schema Utilities
Converts JSON Schema dicts stored on CustomAgent.output_schema into
runtime Pydantic BaseModel classes for structured output enforcement.
"""

import logging
from typing import Any, Optional

from pydantic import BaseModel, create_model

logger = logging.getLogger(__name__)

# Mapping from JSON Schema type strings to Python types
_TYPE_MAP: dict[str, type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def json_schema_to_pydantic(
    schema: dict,
    class_name: str = "AgentOutput",
) -> type[BaseModel]:
    """
    Convert a JSON Schema dict to a Pydantic model class at runtime.

    Supports flat and one-level-nested schemas with basic types.
    For deeply nested schemas, falls back to dict/list types.

    Args:
        schema: JSON Schema dict with "properties" and optionally "required"
        class_name: Name for the generated Pydantic class

    Returns:
        A dynamically created Pydantic BaseModel subclass

    Example:
        schema = {
            "type": "object",
            "properties": {
                "items": {"type": "array", "items": {"type": "string"}},
                "total_cost": {"type": "number"},
                "notes": {"type": "string"}
            },
            "required": ["items", "total_cost"]
        }
        Model = json_schema_to_pydantic(schema)
        # Model now has fields: items (list), total_cost (float), notes (Optional[str])
    """
    properties = schema.get("properties", {})
    required_fields = set(schema.get("required", []))

    if not properties:
        logger.warning("Empty schema properties, returning generic BaseModel")
        return create_model(class_name)

    field_definitions: dict[str, Any] = {}

    for field_name, field_schema in properties.items():
        python_type = _resolve_type(field_schema)
        is_required = field_name in required_fields

        if is_required:
            field_definitions[field_name] = (python_type, ...)
        else:
            field_definitions[field_name] = (Optional[python_type], None)

    model = create_model(class_name, **field_definitions)
    return model


def _resolve_type(field_schema: dict) -> type:
    """Resolve a JSON Schema field definition to a Python type."""
    json_type = field_schema.get("type", "string")

    if json_type == "array":
        # Check for typed items
        items_schema = field_schema.get("items", {})
        item_type = _TYPE_MAP.get(items_schema.get("type", "string"), str)
        return list  # Pydantic handles list validation

    if json_type == "object":
        # Nested object — check for properties
        nested_props = field_schema.get("properties")
        if nested_props:
            # Recursively create a nested model
            return json_schema_to_pydantic(field_schema, class_name="NestedObject")
        return dict

    return _TYPE_MAP.get(json_type, str)


def pydantic_to_openai_schema(model: type[BaseModel]) -> dict:
    """
    Convert a Pydantic model class to the OpenAI structured output format.

    Returns a dict suitable for `text.format.json_schema` in the Responses API.
    """
    schema = model.model_json_schema()
    return {
        "type": "json_schema",
        "name": model.__name__,
        "schema": schema,
        "strict": True,
    }
