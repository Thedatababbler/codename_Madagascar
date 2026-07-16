"""Harness executor registry."""

from __future__ import annotations

from typing import Protocol

from orchestra.ir.artifacts import ArtifactEnvelope
from orchestra.ir.nodes import HarnessNodeSpec
from orchestra.runtime.backend import RunContext
from orchestra.runtime.state import NodeExecutionResult


class HarnessExecutor(Protocol):
    async def execute(
        self,
        node: HarnessNodeSpec,
        inputs: dict[str, ArtifactEnvelope],
        context: RunContext,
    ) -> NodeExecutionResult: ...


class HarnessExecutorRegistry:
    def __init__(self) -> None:
        self._executors: dict[str, HarnessExecutor] = {}

    def register(self, harness_id: str, executor: HarnessExecutor) -> None:
        if not harness_id:
            raise ValueError("harness_id must be non-empty")
        if harness_id in self._executors:
            raise ValueError(f"Duplicate harness id: {harness_id}")
        self._executors[harness_id] = executor

    def has(self, harness_id: str) -> bool:
        return harness_id in self._executors

    def ids(self) -> list[str]:
        return sorted(self._executors)

    async def execute(
        self,
        node: HarnessNodeSpec,
        inputs: dict[str, ArtifactEnvelope],
        context: RunContext,
    ) -> NodeExecutionResult:
        try:
            executor = self._executors[node.harness_id]
        except KeyError as exc:
            raise KeyError(f"Unknown harness: {node.harness_id}") from exc
        return await executor.execute(node, inputs, context)
