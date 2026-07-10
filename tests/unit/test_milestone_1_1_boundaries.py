"""Milestone 1.1 backend boundary and telemetry tests."""

from __future__ import annotations

import inspect

import pytest
from pydantic import ValidationError

from orchestra.backends.base import (
    AgentRequest,
    AgentResult,
    AgentRunStatus,
    AgentTraceEvent,
    BackendExecutionContext,
    ModelSpec,
    OutputContract,
)
from orchestra.backends.catalog import KNOWN_BACKEND_CAPABILITIES
from orchestra.backends.errors import (
    BackendCapabilityError,
    BackendInitializationError,
    ModelInvocationError,
    OutputContractValidationError,
    OutputParseError,
)
from orchestra.backends.factory import build_structured_llm_registry
from orchestra.backends.health import healthcheck_used_backends, used_backend_ids
from orchestra.backends.registry import AgentBackendRegistry
from orchestra.backends.structured_llm import StructuredLLMBackend
from orchestra.cli.validate_graph import build_compiler
from orchestra.executors.agent import AgentNodeExecutor
from orchestra.executors.harness import HarnessNodeExecutor
from orchestra.executors.registry import NodeExecutorRegistry
from orchestra.ir.artifacts import ArtifactBundle, create_artifact
from orchestra.ir.compiler import GraphCompilationError, GraphCompiler
from orchestra.ir.contracts import AgentContract, load_contracts
from orchestra.ir.graph import load_graph
from orchestra.ir.nodes import (
    AgentNodeSpec,
    NodeKind,
    SmolagentsCodeBackendConfig,
    StructuredLLMBackendConfig,
)
from orchestra.llm.mock_async import MockAsyncLLMClient
from orchestra.llm.usage import LLMUsage
from orchestra.runtime.backend import RunContext
from orchestra.runtime.checkpoint import CheckpointStore
from orchestra.runtime.limits import RuntimeLimits, RuntimeSemaphores
from orchestra.runtime.native_async import NativeAsyncRuntime
from orchestra.sandbox.mock import MockSandbox
from orchestra.schemas.artifacts import ProblemArtifact, PublicExample
from orchestra.storage.artifacts import FileArtifactStore
from orchestra.storage.events import AppendOnlyEventWriter
from orchestra.telemetry.events import TelemetryEvent


def _contract() -> AgentContract:
    return AgentContract(
        contract_id="direct_coder",
        role="DirectCoder",
        system_prompt_template="Solve.",
        user_prompt_template="{artifacts_json}",
        model="mock-model",
        temperature=0.0,
        max_tokens=64,
        timeout_seconds=30,
        input_schema="ProblemArtifact",
        output_schema="CodeArtifact",
        parser_id="python_code",
        allowed_tools=[],
    )


def _problem_artifact(task_id: str = "t1"):
    problem = ProblemArtifact(
        question_id=task_id,
        title="Echo",
        statement="Echo one value.",
        difficulty="easy",
        platform="synthetic",
        public_examples=[PublicExample(input="1\n", output="1\n")],
    )
    return create_artifact(problem, producer_node_id="__input__", task_id=task_id)


def _run_context(tmp_path) -> RunContext:
    limits = RuntimeLimits()
    return RunContext(
        run_id="run-1",
        task_id="t1",
        run_dir=tmp_path,
        limits=limits,
        semaphores=RuntimeSemaphores(limits),
        contract_hash="c",
    )


def _agent_request(**overrides) -> AgentRequest:
    base = {
        "request_id": "req-1",
        "task_id": "t1",
        "node_id": "solver",
        "role": "solver",
        "instruction": "solve",
        "rendered_context": "ctx",
        "model": ModelSpec(name="mock"),
        "tools": [],
        "max_steps": 1,
        "timeout_seconds": 10.0,
        "output_contract": OutputContract(
            parser_id="python_code", output_schema="CodeArtifact"
        ),
        "backend_config": {"type": "structured_llm", "max_steps": 1},
    }
    base.update(overrides)
    return AgentRequest(**base)


@pytest.mark.asyncio
async def test_backend_trace_propagates_to_node_result(tmp_path):
    client = MockAsyncLLMClient(
        {"direct_coder": ["```python\nprint(1)\n```"]}
    )
    executor = AgentNodeExecutor(
        {"direct_coder": _contract()}, build_structured_llm_registry(client)
    )
    node = AgentNodeSpec(
        node_id="direct_coder",
        node_kind=NodeKind.AGENT,
        contract_id="direct_coder",
        input_slots={"problem": "ProblemArtifact"},
        output_slots={"code": "CodeArtifact"},
    )
    result = await executor.execute(
        node, {"problem": _problem_artifact()}, _run_context(tmp_path)
    )
    assert result.succeeded
    assert result.backend_id == "structured_llm"
    assert result.backend_status is AgentRunStatus.SUCCESS
    assert result.trace_events
    assert result.trace_events[0].event_type == "model_call"
    assert "raw_trace_path" in result.backend_metadata


@pytest.mark.asyncio
async def test_backend_trace_is_written_to_telemetry(tmp_path):
    contracts = load_contracts("configs/contracts")
    graph = load_graph("configs/graphs/b0_direct.yaml")
    compiled = build_compiler("configs/contracts").compile(graph)
    client = MockAsyncLLMClient(
        {"direct_coder": ["```python\n# CORRECT_SOLUTION\nprint(input())\n```"]}
    )
    writer = AppendOnlyEventWriter(tmp_path)
    runtime = NativeAsyncRuntime(
        executors=NodeExecutorRegistry(
            agent_executor=AgentNodeExecutor(
                contracts, build_structured_llm_registry(client)
            ),
            harness_executor=HarnessNodeExecutor(MockSandbox()),
        ),
        artifact_store=FileArtifactStore(tmp_path),
        checkpoint_store=CheckpointStore(tmp_path),
        event_writer=writer,
    )
    initial = _problem_artifact("echo")
    await runtime.execute(
        graph=compiled,
        initial_artifacts=ArtifactBundle(slots={"problem": initial}),
        context=RunContext(
            run_id="test",
            task_id="echo",
            run_dir=tmp_path,
            limits=RuntimeLimits(),
            semaphores=RuntimeSemaphores(RuntimeLimits()),
            contract_hash="contracts",
        ),
    )
    events = [
        TelemetryEvent.model_validate_json(line)
        for line in (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    types = [event.event_type for event in events]
    assert "backend_run_started" in types
    assert "backend_step" in types
    assert "backend_run_completed" in types
    node_done = next(e for e in events if e.event_type == "NODE_COMPLETED")
    assert node_done.metadata.get("backend_id") == "structured_llm"
    assert node_done.metadata.get("backend_status") == "success"


@pytest.mark.asyncio
async def test_backend_status_is_preserved(tmp_path):
    class _FailBackend(StructuredLLMBackend):
        async def run(self, request, context):
            return AgentResult(
                request_id=request.request_id,
                backend_id=self.backend_id,
                status=AgentRunStatus.MODEL_FAILURE,
                error=None,
                usage=LLMUsage(),
                latency_ms=1,
                trace_events=[
                    AgentTraceEvent(event_type="model_call", index=0, summary="failed")
                ],
            )

    registry = AgentBackendRegistry()
    registry.register(_FailBackend(MockAsyncLLMClient({})))
    executor = AgentNodeExecutor({"direct_coder": _contract()}, registry)
    node = AgentNodeSpec(
        node_id="direct_coder",
        contract_id="direct_coder",
        input_slots={"problem": "ProblemArtifact"},
        output_slots={"code": "CodeArtifact"},
    )
    result = await executor.execute(
        node, {"problem": _problem_artifact()}, _run_context(tmp_path)
    )
    assert not result.succeeded
    assert result.backend_status is AgentRunStatus.MODEL_FAILURE
    assert result.backend_id == "structured_llm"


@pytest.mark.asyncio
async def test_agent_executor_preserves_max_steps(tmp_path):
    captured: dict = {}

    class _CaptureBackend(StructuredLLMBackend):
        async def run(self, request, context):
            captured["max_steps"] = request.max_steps
            return await super().run(request, context)

    registry = AgentBackendRegistry()
    registry.register(
        _CaptureBackend(
            MockAsyncLLMClient({"direct_coder": ["```python\nprint(1)\n```"]})
        )
    )
    # Bypass capability gate so we can assert request construction preserves max_steps.
    registry.validate_request = lambda request: None  # type: ignore[method-assign]
    executor = AgentNodeExecutor({"direct_coder": _contract()}, registry)
    node = AgentNodeSpec(
        node_id="direct_coder",
        contract_id="direct_coder",
        backend=StructuredLLMBackendConfig(max_steps=5),
        input_slots={"problem": "ProblemArtifact"},
        output_slots={"code": "CodeArtifact"},
    )
    await executor.execute(
        node, {"problem": _problem_artifact()}, _run_context(tmp_path)
    )
    assert captured["max_steps"] == 5


@pytest.mark.asyncio
async def test_agent_executor_preserves_tools(tmp_path):
    captured: dict = {}

    class _CaptureBackend(StructuredLLMBackend):
        async def run(self, request, context):
            captured["tools"] = list(request.tools)
            return await super().run(request, context)

    registry = AgentBackendRegistry()
    registry.register(
        _CaptureBackend(
            MockAsyncLLMClient({"direct_coder": ["```python\nprint(1)\n```"]})
        )
    )
    registry.validate_request = lambda request: None  # type: ignore[method-assign]
    executor = AgentNodeExecutor({"direct_coder": _contract()}, registry)
    node = AgentNodeSpec(
        node_id="direct_coder",
        contract_id="direct_coder",
        tools=["python_math", "calculator"],
        input_slots={"problem": "ProblemArtifact"},
        output_slots={"code": "CodeArtifact"},
    )
    await executor.execute(
        node, {"problem": _problem_artifact()}, _run_context(tmp_path)
    )
    assert captured["tools"] == ["python_math", "calculator"]


def test_multistep_rejected_by_incapable_backend():
    caps = KNOWN_BACKEND_CAPABILITIES["structured_llm"]
    with pytest.raises(BackendCapabilityError):
        AgentBackendRegistry.validate_capabilities(
            _agent_request(max_steps=3), caps
        )
    contracts = load_contracts("configs/contracts")
    compiler = GraphCompiler(
        contracts=contracts,
        harness_ids={"public_code_harness"},
        transform_ids={
            "identity_code",
            "merge_analysis_artifacts",
            "repair_to_code",
            "freeze_code",
        },
        selector_ids={"deterministic_code_selector"},
        backend_ids={"structured_llm"},
    )
    graph = load_graph("configs/graphs/b0_direct.yaml")
    # Mutate node backend max_steps after load.
    nodes = []
    for node in graph.nodes:
        if isinstance(node, AgentNodeSpec):
            nodes.append(
                node.model_copy(
                    update={"backend": StructuredLLMBackendConfig(max_steps=4)}
                )
            )
        else:
            nodes.append(node)
    bad = graph.model_copy(update={"nodes": nodes})
    with pytest.raises(GraphCompilationError, match="multi-step"):
        compiler.compile(bad)


def test_tools_rejected_by_incapable_backend():
    caps = KNOWN_BACKEND_CAPABILITIES["structured_llm"]
    with pytest.raises(BackendCapabilityError, match="tools"):
        AgentBackendRegistry.validate_capabilities(
            _agent_request(tools=["calculator"]), caps
        )
    contracts = load_contracts("configs/contracts")
    compiler = GraphCompiler(
        contracts=contracts,
        harness_ids={"public_code_harness"},
        transform_ids={
            "identity_code",
            "merge_analysis_artifacts",
            "repair_to_code",
            "freeze_code",
        },
        selector_ids={"deterministic_code_selector"},
        backend_ids={"structured_llm"},
    )
    graph = load_graph("configs/graphs/b0_direct.yaml")
    nodes = []
    for node in graph.nodes:
        if isinstance(node, AgentNodeSpec):
            nodes.append(node.model_copy(update={"tools": ["calculator"]}))
        else:
            nodes.append(node)
    bad = graph.model_copy(update={"nodes": nodes})
    with pytest.raises(GraphCompilationError, match="tools"):
        compiler.compile(bad)


def test_backend_context_does_not_expose_run_context():
    fields = set(BackendExecutionContext.model_fields)
    forbidden = {
        "run_context",
        "artifact_store",
        "checkpoint_store",
        "semaphores",
        "limits",
    }
    assert fields.isdisjoint(forbidden)
    ctx = BackendExecutionContext(
        run_id="r",
        task_id="t",
        node_id="n",
        artifact_refs=[],
        trace_dir="/tmp/traces",
    )
    assert not hasattr(ctx, "run_context")
    source = inspect.getsource(AgentNodeExecutor.execute)
    assert "BackendExecutionContext(" in source
    assert "run_context=" not in source.split("BackendExecutionContext(")[1].split(")")[0]


def test_output_errors_use_typed_exceptions():
    backend = StructuredLLMBackend(MockAsyncLLMClient({}))
    source = inspect.getsource(StructuredLLMBackend.run)
    assert "Parser" not in source or 'raise OutputParseError' in source
    assert '"Parser" in str' not in source
    assert '"ValidationError" in str' not in source
    assert issubclass(ModelInvocationError, Exception)
    assert issubclass(OutputParseError, Exception)
    assert issubclass(OutputContractValidationError, Exception)
    assert issubclass(BackendInitializationError, Exception)

    # Status mapping without string matching on exception messages.
    failure = backend._failure(
        _agent_request(),
        AgentRunStatus.ACTION_PARSE_FAILURE,
        OutputParseError("bad json"),
        1,
    )
    assert failure.status is AgentRunStatus.ACTION_PARSE_FAILURE
    assert "OutputParseError" in failure.error.message


@pytest.mark.asyncio
async def test_used_backend_healthcheck_runs_before_tasks(tmp_path):
    graph = load_graph("configs/graphs/b0_direct.yaml")
    compiled = build_compiler("configs/contracts").compile(graph)
    assert used_backend_ids(compiled) == ["structured_llm"]
    registry = build_structured_llm_registry(MockAsyncLLMClient({}))
    writer = AppendOnlyEventWriter(tmp_path)
    await healthcheck_used_backends(
        registry=registry,
        graph=compiled,
        event_writer=writer,
        run_id="run-hc",
        graph_id=graph.graph_id,
    )
    events = [
        TelemetryEvent.model_validate_json(line)
        for line in (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    types = [e.event_type for e in events]
    assert types == [
        "backend_healthcheck_started",
        "backend_healthcheck_passed",
    ]


@pytest.mark.asyncio
async def test_failed_healthcheck_fails_closed(tmp_path):
    graph = load_graph("configs/graphs/b0_direct.yaml")
    compiled = build_compiler("configs/contracts").compile(graph)

    class _Unhealthy(StructuredLLMBackend):
        async def healthcheck(self):
            from orchestra.backends.base import BackendHealth

            return BackendHealth(
                healthy=False, backend_id=self.backend_id, detail="missing deps"
            )

    registry = AgentBackendRegistry()
    registry.register(_Unhealthy(MockAsyncLLMClient({})))
    writer = AppendOnlyEventWriter(tmp_path)
    with pytest.raises(BackendInitializationError, match="missing deps"):
        await healthcheck_used_backends(
            registry=registry,
            graph=compiled,
            event_writer=writer,
            run_id="run-hc-fail",
            graph_id=graph.graph_id,
        )
    events = [
        TelemetryEvent.model_validate_json(line)
        for line in (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert any(e.event_type == "backend_healthcheck_failed" for e in events)


def test_smolagents_config_rejects_managed_agents():
    with pytest.raises(ValidationError, match="managed_agents"):
        SmolagentsCodeBackendConfig.model_validate(
            {"type": "smolagents_code", "managed_agents": [{"name": "nested"}]}
        )
    cfg = SmolagentsCodeBackendConfig()
    assert cfg.type == "smolagents_code"
    assert cfg.max_steps == 8
    assert cfg.managed_agents is None
