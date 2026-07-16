import json
import time
from pathlib import Path
from uuid import uuid4

from orchestra.backends.base import (
    AgentRequest,
    AgentRunStatus,
    AgentSessionPolicy,
    ArtifactRef,
    BackendExecutionContext,
    ModelSpec,
    OutputContract,
)
from orchestra.backends.errors import BackendCapabilityError
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
        if len(node.output_slots) != 1:
            raise ValueError("Agent nodes must declare exactly one output slot")
        messages = render_contract(contract, inputs)
        backend = node.resolved_backend()
        max_steps = getattr(backend, "max_steps", 1)
        tools = list(node.tools) if node.tools else list(contract.allowed_tools)
        if node.model is not None:
            model = node.model
        else:
            model = ModelSpec(
                name=contract.model,
                temperature=contract.temperature,
                max_tokens=contract.max_tokens,
            )
        if node.output_contract is not None:
            output_contract = node.output_contract
        else:
            output_contract = OutputContract(
                parser_id=contract.parser_id,
                output_schema=contract.output_schema,
            )
        return AgentRequest(
            request_id=str(uuid4()),
            task_id=context.task_id,
            subtask_id=context.subtask_id,
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
            model=model,
            tools=tools,
            max_steps=max_steps,
            timeout_seconds=node.timeout_seconds or contract.timeout_seconds,
            output_contract=output_contract,
            backend_config=backend.model_dump(mode="json"),
            messages=messages,
            contract_id=contract.contract_id,
            session_policy=AgentSessionPolicy.FRESH,
            session_ref=None,
        )

    def _trace_dir(self, context: RunContext, node_id: str) -> str:
        path = Path(context.run_dir) / "tasks" / context.task_id / "backend_traces" / node_id
        path.mkdir(parents=True, exist_ok=True)
        return str(path)

    def _persist_raw_trace(
        self, trace_dir: str, request_id: str, result
    ) -> str | None:
        if not result.trace_events:
            return None
        path = Path(trace_dir) / f"{request_id}.json"
        path.write_text(
            json.dumps(
                [event.model_dump(mode="json") for event in result.trace_events],
                indent=2,
            ),
            encoding="utf-8",
        )
        return str(path)

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
        trace_dir = self._trace_dir(context, node.node_id)
        backend_context = BackendExecutionContext(
            run_id=context.run_id,
            task_id=context.task_id,
            subtask_id=context.subtask_id,
            node_id=node.node_id,
            artifact_refs=request.input_artifacts,
            trace_dir=trace_dir,
            workspace_ref=context.workspace_ref,
        )
        # Semaphores stay in the control plane; backends never see them.
        async with context.semaphores.llm:
            result = await backend.run(request, backend_context)
        raw_trace_path = self._persist_raw_trace(trace_dir, request.request_id, result)
        metadata = dict(result.backend_metadata)
        if result.session_ref is not None:
            metadata["session_ref"] = result.session_ref.model_dump(mode="json")
        if raw_trace_path:
            metadata["raw_trace_path"] = raw_trace_path
            metadata["trace_summary"] = [
                {
                    "event_type": event.event_type,
                    "index": event.index,
                    "summary": event.summary or event.message,
                }
                for event in result.trace_events
            ]
        latency_ms = result.latency_ms or int((time.perf_counter() - started) * 1000)
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
                latency_ms=latency_ms,
                usage=result.usage,
                backend_id=result.backend_id,
                backend_status=result.status,
                trace_events=result.trace_events,
                backend_metadata=metadata,
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
            latency_ms=latency_ms,
            usage=result.usage,
            backend_id=result.backend_id,
            backend_status=result.status,
            trace_events=result.trace_events,
            backend_metadata=metadata,
        )

    async def execute_safely(self, *args, **kwargs) -> NodeExecutionResult:
        node = args[0] if args else kwargs["node"]
        started = time.perf_counter()
        try:
            return await self.execute(*args, **kwargs)
        except (ValueError, KeyError, TimeoutError, BackendCapabilityError) as exc:
            return NodeExecutionResult.failed(
                node.node_id, exc, int((time.perf_counter() - started) * 1000)
            )
