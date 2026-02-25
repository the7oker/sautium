"""Tool registry and definitions for multi-provider LLM support."""

from tools.registry import REGISTRY, ToolRegistry, ToolDef, ToolParam

__all__ = ["REGISTRY", "ToolRegistry", "ToolDef", "ToolParam"]

_definitions_loaded = False


def ensure_definitions():
    """Load tool definitions if not yet loaded. Called lazily by providers."""
    global _definitions_loaded
    if not _definitions_loaded:
        _definitions_loaded = True
        import tools.definitions  # noqa: F401
