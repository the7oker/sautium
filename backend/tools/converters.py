"""Convert tool definitions to LLM provider formats."""

from tools.registry import ToolRegistry, ToolDef, ToolParam


def _param_to_json_schema(p: ToolParam) -> dict:
    """Convert a ToolParam to JSON Schema property."""
    type_map = {
        "string": "string",
        "integer": "integer",
        "number": "number",
        "boolean": "boolean",
        "array": "array",
    }
    schema: dict = {"type": type_map.get(p.type, "string"), "description": p.description}
    if p.enum:
        schema["enum"] = p.enum
    if p.type == "array" and p.items_type:
        schema["items"] = {"type": type_map.get(p.items_type, "string")}
    return schema


def _tool_to_schema(tool: ToolDef) -> dict:
    """Build JSON Schema for tool parameters."""
    if not tool.parameters:
        return {"type": "object", "properties": {}, "required": []}

    properties = {}
    required = []
    for p in tool.parameters:
        properties[p.name] = _param_to_json_schema(p)
        if p.required:
            required.append(p.name)

    return {
        "type": "object",
        "properties": properties,
        "required": required,
    }


def to_anthropic_tools(registry: ToolRegistry) -> list[dict]:
    """Convert registry to Anthropic tool_use format.

    Returns list of dicts compatible with anthropic SDK's `tools` parameter.
    """
    tools = []
    for tool in registry.all():
        tools.append({
            "name": tool.name,
            "description": tool.description,
            "input_schema": _tool_to_schema(tool),
        })
    return tools


def to_openai_tools(registry: ToolRegistry) -> list[dict]:
    """Convert registry to OpenAI function calling format.

    Returns list of dicts compatible with openai SDK's `tools` parameter.
    """
    tools = []
    for tool in registry.all():
        tools.append({
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": _tool_to_schema(tool),
            },
        })
    return tools
