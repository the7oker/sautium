"""Execute tools by name from the global registry."""

import json
import logging
from typing import Any

from tools.registry import REGISTRY

logger = logging.getLogger(__name__)


def execute_tool(name: str, arguments: dict[str, Any]) -> str:
    """Find tool in REGISTRY and call its handler.

    Args:
        name: Tool name (e.g. "search_tracks")
        arguments: Dict of arguments to pass to handler

    Returns:
        Tool result as string
    """
    # Ensure definitions are loaded
    from tools import ensure_definitions
    ensure_definitions()

    tool = REGISTRY.get(name)
    if tool is None:
        return f"Error: Unknown tool '{name}'"

    if tool.handler is None:
        return f"Error: Tool '{name}' has no handler"

    try:
        # Convert argument types based on tool parameter definitions
        typed_args = {}
        for param in tool.parameters:
            if param.name in arguments:
                val = arguments[param.name]
                typed_args[param.name] = _coerce_type(val, param.type)
            elif not param.required and param.default is not None:
                typed_args[param.name] = param.default

        result = tool.handler(**typed_args)
        return str(result) if result is not None else "OK"
    except Exception as e:
        logger.error(f"Tool '{name}' failed: {e}", exc_info=True)
        return f"Error executing tool '{name}': {e}"


def _coerce_type(value: Any, target_type: str) -> Any:
    """Coerce a value to the expected type."""
    if value is None:
        return value

    if target_type == "integer":
        return int(value)
    elif target_type == "number":
        return float(value)
    elif target_type == "boolean":
        if isinstance(value, str):
            return value.lower() in ("true", "1", "yes")
        return bool(value)
    elif target_type == "array":
        if isinstance(value, str):
            return json.loads(value)
        return value
    elif target_type == "string":
        return str(value)
    return value
