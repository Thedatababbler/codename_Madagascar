"""Default tool registry singleton helpers."""

from orchestra.tools.base import ToolRegistry, default_tool_registry

_REGISTRY: ToolRegistry | None = None


def get_tool_registry() -> ToolRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = default_tool_registry()
    return _REGISTRY


def reset_tool_registry_for_tests() -> None:
    global _REGISTRY
    _REGISTRY = None
