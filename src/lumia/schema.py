"""Tiny builders for JSON-Schema tool definitions.

Kept apart from `tools.py` so every tool module — growth and
communication alike — can describe its inputs without importing the
toolbox that will register them.
"""

from __future__ import annotations

from typing import Any


def obj(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
    }


def string(description: str, enum: list[str] | None = None) -> dict[str, Any]:
    spec: dict[str, Any] = {"type": "string", "description": description}
    if enum:
        spec["enum"] = enum
    return spec


def number(description: str) -> dict[str, Any]:
    return {"type": "number", "description": description}


def integer(description: str) -> dict[str, Any]:
    return {"type": "integer", "description": description}


def boolean(description: str) -> dict[str, Any]:
    return {"type": "boolean", "description": description}


def array(description: str, item_type: str = "string") -> dict[str, Any]:
    return {"type": "array", "description": description, "items": {"type": item_type}}
