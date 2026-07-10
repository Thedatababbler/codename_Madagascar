"""Tool registry abstractions."""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field


class ToolBuildContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    run_id: str | None = None
    task_id: str | None = None
    node_id: str | None = None
    workspace_ref: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ToolFactory(Protocol):
    def __call__(self, context: ToolBuildContext) -> Any: ...


class UnknownToolError(ValueError):
    pass


class ToolRegistry:
    def __init__(self) -> None:
        self._factories: dict[str, ToolFactory] = {}

    def register(self, tool_id: str, factory: ToolFactory) -> None:
        if not tool_id:
            raise ValueError("tool_id must be non-empty")
        if tool_id in self._factories:
            raise ValueError(f"Duplicate tool id: {tool_id}")
        self._factories[tool_id] = factory

    def known_ids(self) -> set[str]:
        return set(self._factories)

    def has(self, tool_id: str) -> bool:
        return tool_id in self._factories

    def build(
        self,
        tool_ids: list[str],
        context: ToolBuildContext,
    ) -> list[Any]:
        tools: list[Any] = []
        for tool_id in tool_ids:
            if tool_id not in self._factories:
                raise UnknownToolError(f"Unknown tool id: {tool_id}")
            tools.append(self._factories[tool_id](context))
        return tools


def default_tool_registry() -> ToolRegistry:
    from orchestra.tools.bbeh_tools import register_bbeh_tools

    registry = ToolRegistry()
    register_bbeh_tools(registry)
    return registry
