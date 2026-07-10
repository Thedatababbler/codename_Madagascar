import asyncio
import time

from orchestra.executors.agent import AgentNodeExecutor
from orchestra.executors.harness import HarnessNodeExecutor
from orchestra.executors.selector import SelectorExecutor
from orchestra.executors.transform import TransformExecutor
from orchestra.ir.artifacts import ArtifactEnvelope
from orchestra.ir.nodes import (
    AgentNodeSpec,
    HarnessNodeSpec,
    NodeSpec,
    SelectorNodeSpec,
    TransformNodeSpec,
)
from orchestra.runtime.backend import RunContext
from orchestra.runtime.state import NodeExecutionResult


class NodeExecutorRegistry:
    def __init__(
        self,
        *,
        agent_executor: AgentNodeExecutor,
        harness_executor: HarnessNodeExecutor,
        transform_executor: TransformExecutor | None = None,
        selector_executor: SelectorExecutor | None = None,
    ) -> None:
        self.agent = agent_executor
        self.harness = harness_executor
        self.transform = transform_executor or TransformExecutor()
        self.selector = selector_executor or SelectorExecutor()

    async def execute_safely(
        self,
        node: NodeSpec,
        inputs: dict[str, ArtifactEnvelope],
        context: RunContext,
    ) -> NodeExecutionResult:
        started = time.perf_counter()
        try:
            async with asyncio.timeout(node.timeout_seconds):
                if isinstance(node, AgentNodeSpec):
                    return await self.agent.execute(node, inputs, context)
                if isinstance(node, HarnessNodeSpec):
                    return await self.harness.execute(node, inputs, context)
                if isinstance(node, TransformNodeSpec):
                    return await self.transform.execute(node, inputs, context)
                if isinstance(node, SelectorNodeSpec):
                    return await self.selector.execute(node, inputs, context)
                raise TypeError(type(node))
        except (ValueError, KeyError, TimeoutError) as exc:
            return NodeExecutionResult.failed(
                node.node_id, exc, int((time.perf_counter() - started) * 1000)
            )
