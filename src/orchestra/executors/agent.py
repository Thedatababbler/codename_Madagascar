import time
from uuid import uuid4

from orchestra.backends.base import (
    AgentRequest,
    AgentRunStatus,
    ArtifactRef,
    BackendExecutionContext,
    ModelSpec,
    OutputContract,
)
from orchestra.backends.registry import AgentBackendRegistry
from orchestra.ir.artifacts import ArtifactEnvelope
from orchestra.ir.contracts import AgentContract
from orchestra.ir.nodes import AgentNodeSpec
from orchestra.prompts.render import render_contract
from orchestra.runtime.backend import RunContext
from orchestra.runtime.state import NodeExecutionResult


class AgentNodeExecutor:
    """Control-plane agent node executor that delegates to AgentBackendRegistry."""

    def __init__(
        self,
        contracts: dict[str, AgentContract],
        backends: AgentBackendRegistry,
    ) -> None:
        self.contracts = contracts
        self.backends = backends

    def _build_request(
        self,
        node: AgentNodeSpec,
        inputs: dict[str, ArtifactEnvelope],
        context: RunContext,
    ) -> AgentRequest:
        contract = self.contracts[node.contract_id]
        if contract.allowed_tools:
            raise ValueError("Stage 1 agent contracts may not use arbitrary tools")
        if len(node.output_slots) != 1:
            raise ValueError("Stage 1 agent nodes must declare exactly one output slot")
        messages = render_contract(contract, inputs)
        backend = node.resolved_backend()
        return AgentRequest(
            request_id=str(uuid4()),
            task_id=context.task_id,
            node_id=node.node_id,
            role=contract.role,
            instruction=contract.system_prompt_template,
            input_artifacts=[
                ArtifactRef(
                    slot=slot,
                    artifact_id=artifact.artifact_id,
                    artifact_type=artifact.artifact_type,
                )
                for slot, artifact in sorted(inputs.items())
            ],
            rendered_context=messages[-1]["content"] if messages else "",
            model=ModelSpec(
                name=contract.model,
                temperature=contract.temperature,
                max_tokens=contract.max_tokens,
            ),
            tools=list(contract.allowed_tools),
            max_steps=1,
            timeout_seconds=node.timeout_seconds or contract.timeout_seconds,
            output_contract=OutputContract(
                parser_id=contract.parser_id,
                output_schema=contract.output_schema,
            ),
            backend_config=backend.model_dump(mode="json"),
            messages=messages,
            contract_id=contract.contract_id,
        )

    async def execute(
        self,
        node: AgentNodeSpec,
        inputs: dict[str, ArtifactEnvelope],
        context: RunContext,
    ) -> NodeExecutionResult:
        started = time.perf_counter()
        request = self._build_request(node, inputs, context)
        self.backends.validate_request(request)
        backend = self.backends.get(str(request.backend_config["type"]))
        result = await backend.run(
            request,
            BackendExecutionContext(run_context=context, input_envelopes=inputs),
        )
        if result.status is not AgentRunStatus.SUCCESS or not result.output_artifacts:
            message = (
                result.error.message
                if result.error is not None
                else f"Backend returned status {result.status}"
            )
            return NodeExecutionResult(
                node_id=node.node_id,
                succeeded=False,
                error=message,
                latency_ms=result.latency_ms
                or int((time.perf_counter() - started) * 1000),
                usage=result.usage,
            )
        output_slot, expected_schema = next(iter(node.output_slots.items()))
        artifact = result.output_artifacts[0]
        if artifact.artifact_type != expected_schema:
            raise ValueError(
                f"Backend produced {artifact.artifact_type}, expected {expected_schema}"
            )
        return NodeExecutionResult(
            node_id=node.node_id,
            succeeded=True,
            outputs={output_slot: artifact},
            latency_ms=result.latency_ms
            or int((time.perf_counter() - started) * 1000),
            usage=result.usage,
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
