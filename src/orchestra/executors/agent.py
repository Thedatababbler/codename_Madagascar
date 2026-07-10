import time

from orchestra.ir.artifacts import ArtifactEnvelope, create_artifact
from orchestra.ir.contracts import AgentContract
from orchestra.ir.nodes import AgentNodeSpec
from orchestra.llm.base_async import AsyncLLMClient
from orchestra.prompts.parsers import parse_output
from orchestra.prompts.render import render_contract
from orchestra.runtime.backend import RunContext
from orchestra.runtime.state import NodeExecutionResult


class AgentNodeExecutor:
    def __init__(
        self,
        client: AsyncLLMClient,
        contracts: dict[str, AgentContract],
    ) -> None:
        self.client = client
        self.contracts = contracts

    async def execute(
        self,
        node: AgentNodeSpec,
        inputs: dict[str, ArtifactEnvelope],
        context: RunContext,
    ) -> NodeExecutionResult:
        started = time.perf_counter()
        contract = self.contracts[node.contract_id]
        if contract.allowed_tools:
            raise ValueError("Stage 1 agent contracts may not use arbitrary tools")
        async with context.semaphores.llm:
            response = await self.client.generate(
                messages=render_contract(contract, inputs),
                model=contract.model,
                temperature=contract.temperature,
                max_tokens=contract.max_tokens,
                timeout_seconds=node.timeout_seconds or contract.timeout_seconds,
                metadata={
                    "run_id": context.run_id,
                    "task_id": context.task_id,
                    "node_id": node.node_id,
                    "contract_id": contract.contract_id,
                },
            )
        parsed = parse_output(
            contract.parser_id, response.text, contract.output_schema, node.node_id
        )
        if len(node.output_slots) != 1:
            raise ValueError("Stage 1 agent nodes must declare exactly one output slot")
        output_slot, expected_schema = next(iter(node.output_slots.items()))
        if type(parsed).__name__ != expected_schema:
            raise ValueError(
                f"Parser produced {type(parsed).__name__}, expected {expected_schema}"
            )
        artifact = create_artifact(
            parsed,
            producer_node_id=node.node_id,
            task_id=context.task_id,
            parent_artifact_ids=[item.artifact_id for item in inputs.values()],
        )
        return NodeExecutionResult(
            node_id=node.node_id,
            succeeded=True,
            outputs={output_slot: artifact},
            latency_ms=int((time.perf_counter() - started) * 1000),
            usage=response.usage,
        )

    async def execute_safely(self, *args, **kwargs) -> NodeExecutionResult:
        node = args[0] if args else kwargs["node"]
        started = time.perf_counter()
        try:
            return await self.execute(*args, **kwargs)
        except (ValueError, KeyError, TimeoutError) as exc:
            return NodeExecutionResult.failed(
                node.node_id, exc, int((time.perf_counter() - started) * 1000)
            )
