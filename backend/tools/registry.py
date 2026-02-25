"""Tool registry for LLM function calling."""

from dataclasses import dataclass, field
from typing import Any, Callable, Optional


@dataclass
class ToolParam:
    """Definition of a single tool parameter."""
    name: str
    type: str  # "string", "number", "integer", "boolean", "array"
    description: str
    required: bool = True
    default: Any = None
    enum: Optional[list] = None
    items_type: Optional[str] = None  # for array type


@dataclass
class ToolDef:
    """Definition of a tool with its handler."""
    name: str
    description: str
    parameters: list[ToolParam] = field(default_factory=list)
    handler: Optional[Callable[..., str]] = None


class ToolRegistry:
    """Registry of available tools."""

    def __init__(self):
        self._tools: dict[str, ToolDef] = {}

    def register(self, tool: ToolDef) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[ToolDef]:
        return self._tools.get(name)

    def all(self) -> list[ToolDef]:
        return list(self._tools.values())

    def names(self) -> list[str]:
        return list(self._tools.keys())


# Global singleton
REGISTRY = ToolRegistry()
