"""Schema conversion between AINDY's lightweight format and JSON Schema.

AINDY lightweight format
------------------------
A simplified dict used in syscall registration::

    {
        "required": ["content"],
        "properties": {
            "content": {"type": "string"},
            "limit":   {"type": "int"},
            "tags":    {"type": "list"},
        }
    }

JSON Schema (what MCP expects)
-------------------------------
Full JSON Schema Draft 7 object format::

    {
        "type": "object",
        "properties": {
            "content": {"type": "string"},
            "limit":   {"type": "integer"},
            "tags":    {"type": "array"},
        },
        "required": ["content"],
    }
"""
from __future__ import annotations

from typing import Any

# Normalise AINDY type aliases → JSON Schema type names
_TYPE_MAP: dict[str, str] = {
    "str":     "string",
    "string":  "string",
    "int":     "integer",
    "integer": "integer",
    "float":   "number",
    "number":  "number",
    "bool":    "boolean",
    "boolean": "boolean",
    "list":    "array",
    "array":   "array",
    "dict":    "object",
    "object":  "object",
}


def lightweight_to_json_schema(schema: dict[str, Any] | None) -> dict[str, Any]:
    """Convert AINDY's lightweight schema to a JSON Schema object.

    Args:
        schema: Lightweight schema dict or None.

    Returns:
        A JSON Schema ``{"type": "object", ...}`` dict.  Returns
        ``{"type": "object", "properties": {}}`` when *schema* is None or empty.
    """
    if not schema:
        return {"type": "object", "properties": {}}

    properties: dict[str, Any] = {}
    raw_props = schema.get("properties") or {}

    for name, spec in raw_props.items():
        if not isinstance(spec, dict):
            properties[name] = {"type": "string"}
            continue
        prop: dict[str, Any] = {}
        raw_type = str(spec.get("type", "string"))
        prop["type"] = _TYPE_MAP.get(raw_type, raw_type)
        if "description" in spec:
            prop["description"] = spec["description"]
        # Preserve any extra fields (e.g., "enum", "default")
        for key, val in spec.items():
            if key not in ("type", "description"):
                prop[key] = val
        properties[name] = prop

    result: dict[str, Any] = {"type": "object", "properties": properties}

    required = schema.get("required")
    if required:
        result["required"] = list(required)

    return result


def json_schema_to_lightweight(schema: dict[str, Any]) -> dict[str, Any]:
    """Convert a JSON Schema object back to AINDY's lightweight format.

    Args:
        schema: A JSON Schema object (``{"type": "object", "properties": {...}}``)

    Returns:
        Lightweight schema dict compatible with AINDY's schema validators.
    """
    if not schema or schema.get("type") != "object":
        return {}

    properties: dict[str, Any] = {}
    raw_props = schema.get("properties") or {}

    # Canonical reverse map: JSON Schema type → preferred AINDY lightweight type
    _reverse: dict[str, str] = {
        "string":  "string",
        "integer": "int",
        "number":  "float",
        "boolean": "bool",
        "array":   "list",
        "object":  "dict",
    }

    for name, spec in raw_props.items():
        if not isinstance(spec, dict):
            properties[name] = {"type": "string"}
            continue
        prop: dict[str, Any] = {}
        json_type = str(spec.get("type", "string"))
        prop["type"] = _reverse.get(json_type, json_type)
        if "description" in spec:
            prop["description"] = spec["description"]
        for key, val in spec.items():
            if key not in ("type", "description"):
                prop[key] = val
        properties[name] = prop

    result: dict[str, Any] = {"properties": properties}

    required = schema.get("required")
    if required:
        result["required"] = list(required)

    return result
