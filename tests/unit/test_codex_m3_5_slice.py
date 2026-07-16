"""Milestone 3.5 Codex backend / workspace / harness tests."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from pydantic import ValidationError

openai_codex = pytest.importorskip("openai_codex")

from orchestra.backends.base import (
    AgentRequest,
    AgentRunStatus,
    AgentSessionPolicy,
    BackendExecutionContext,
    BackendSessionRef,
    ModelSpec,
    OutputContract,
)
from orchestra.backends.codex_sdk import CodexSDKBackend
from orchestra.backends.factory import build_default_backend_registry
from orchestra.backends.registry import AgentBackendRegistry
from orchestra.cli.validate_graph import build_compiler
from orchestra.control.single_subtask import SingleSubtaskCompatibilityRunner
from orchestra.control.task_state import SubtaskStatus
from orchestra.decomposition.schemas import TaskPlan
from orchestra.executors.agent import AgentNodeExecutor
from orchestra.executors.harness import HarnessNodeExecutor
from orchestra.executors.registry import NodeExecutorRegistry
from orchestra.harness.repository_test import RepositoryTestHarnessExecutor
from orchestra.ir.artifacts import ArtifactBundle, create_artifact
from orchestra.ir.contracts import load_contracts
from orchestra.ir.graph import load_graph
from orchestra.ir.nodes import CodexSDKBackendConfig, HarnessNodeSpec
from orchestra.llm.mock_async import MockAsyncLLMClient
from orchestra.runtime.backend import RunContext
from orchestra.runtime.checkpoint import CheckpointStore
from orchestra.runtime.limits import RuntimeLimits, RuntimeSemaphores
from orchestra.runtime.native_async import NativeAsyncRuntime
from orchestra.runtime.task_checkpoint import TaskCheckpointStore
from orchestra.sandbox.mock import MockSandbox
from orchestra.schemas.artifacts import ProblemArtifact, RepositoryChangeArtifact
from orchestra.storage.artifacts import FileArtifactStore
from orchestra.storage.events import AppendOnlyEventWriter
from orchestra.workspaces.git_workspace import SharedSubtaskGitWorkspaceManager

FIXTURE = Path("tests/fixtures/codex_tiny_repo").resolve()
CONTRACTS = "configs/contracts"


def _request(**overrides) -> AgentRequest:
    base = {
        "request_id": "r1",
        "task_id": "codex_tiny_repo",
        "subtask_id": "implement_fix",
        "node_id": "codex_implementer",
        "role": "CodexImplementer",
        "instruction": "Fix tests",
        "rendered_context": "Fix the repository so tests pass.",
        "model": ModelSpec(name="gpt-5.4"),
        "tools": [],
        "max_steps": 1,
        "timeout_seconds": 30.0,
        "output_contract": OutputContract(
            parser_id="repository_change",
            output_schema="RepositoryChangeArtifact",
        ),
        "backend_config": {
            "type": "codex_sdk",
            "thread_policy": "fresh",
            "sandbox": "workspace_write",
            "approval_policy": "never",
            "require_git_diff": True,
        },
        "session_policy": AgentSessionPolicy.FRESH,
    }
    base.update(overrides)
    return AgentRequest(**base)


class _FakeTurn:
    final_response = "fixed add"
    usage = SimpleNamespace(input_tokens=1, output_tokens=2)


class _FakeThread:
    id = "thread-fake-1"

    def __init__(self, workspace: str) -> None:
        self.workspace = workspace

    async def run(self, prompt: str, **kwargs):  # noqa: ANN003
        del prompt, kwargs
        path = Path(self.workspace) / "calculator.py"
        path.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        return _FakeTurn()


class _FakeCodex:
    def __init__(self) -> None:
        self._cwd: str | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):  # noqa: ANN001
        return None

    async def thread_start(self, **kwargs):  # noqa: ANN003
        self._cwd = kwargs.get("cwd")
        return _FakeThread(self._cwd)


def test_codex_backend_config_forbids_full_access():
    with pytest.raises(ValidationError):
        CodexSDKBackendConfig.model_validate(
            {"type": "codex_sdk", "sandbox": "full_access"}
        )


def test_codex_backend_config_fresh_only():
    cfg = CodexSDKBackendConfig()
    assert cfg.thread_policy == "fresh"
    assert cfg.approval_policy == "never"


@pytest.mark.asyncio
async def test_codex_backend_requires_workspace():
    backend = CodexSDKBackend(client_factory=lambda: _FakeCodex())
    result = await backend.run(
        _request(),
        BackendExecutionContext(
            run_id="run",
            task_id="codex_tiny_repo",
            subtask_id="implement_fix",
            node_id="codex_implementer",
            workspace_ref=None,
        ),
    )
    assert result.status is AgentRunStatus.INVALID_REQUEST
    assert "workspace_ref" in (result.error.message if result.error else "")


@pytest.mark.asyncio
async def test_codex_backend_no_silent_fallback(tmp_path):
    registry = build_default_backend_registry(
        MockAsyncLLMClient({}), include_smolagents=False, include_codex=True
    )
    assert registry.has("codex_sdk")
    backend = registry.get("codex_sdk")
    result = await backend.run(
        _request(session_policy=AgentSessionPolicy.RESUME),
        BackendExecutionContext(
            run_id="run",
            task_id="t",
            node_id="n",
            workspace_ref=str(tmp_path),
        ),
    )
    assert result.status is AgentRunStatus.INVALID_REQUEST


@pytest.mark.asyncio
async def test_codex_backend_healthcheck():
    backend = CodexSDKBackend(client_factory=lambda: _FakeCodex())
    health = await backend.healthcheck()
    assert health.backend_id == "codex_sdk"
    assert health.healthy is True


@pytest.mark.asyncio
async def test_workspace_isolation(tmp_path):
    mgr = SharedSubtaskGitWorkspaceManager()
    ws_a = await mgr.prepare(
        source_repo=str(FIXTURE),
        run_dir=str(tmp_path),
        task_id="task_a",
        subtask_id="implement_fix",
    )
    ws_b = await mgr.prepare(
        source_repo=str(FIXTURE),
        run_dir=str(tmp_path),
        task_id="task_b",
        subtask_id="implement_fix",
    )
    assert ws_a.path != ws_b.path
    (Path(ws_a.path) / "calculator.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8"
    )
    assert "a - b" in (Path(ws_b.path) / "calculator.py").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_codex_repository_artifact(tmp_path):
    mgr = SharedSubtaskGitWorkspaceManager()
    ws = await mgr.prepare(
        source_repo=str(FIXTURE),
        run_dir=str(tmp_path),
        task_id="codex_tiny_repo",
        subtask_id="implement_fix",
    )
    backend = CodexSDKBackend(client_factory=lambda: _FakeCodex())
    result = await backend.run(
        _request(),
        BackendExecutionContext(
            run_id="run",
            task_id="codex_tiny_repo",
            subtask_id="implement_fix",
            node_id="codex_implementer",
            workspace_ref=ws.path,
        ),
    )
    assert result.status is AgentRunStatus.SUCCESS
    assert result.session_ref is not None
    assert result.session_ref.session_id == "thread-fake-1"
    art = result.output_artifacts[0]
    assert art.artifact_type == "RepositoryChangeArtifact"
    payload = RepositoryChangeArtifact.model_validate(art.payload)
    assert payload.patch.strip()
    assert "calculator.py" in payload.changed_files


@pytest.mark.asyncio
async def test_repository_harness(tmp_path):
    mgr = SharedSubtaskGitWorkspaceManager()
    ws = await mgr.prepare(
        source_repo=str(FIXTURE),
        run_dir=str(tmp_path),
        task_id="codex_tiny_repo",
        subtask_id="implement_fix",
    )
    (Path(ws.path) / "calculator.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8"
    )
    change = create_artifact(
        RepositoryChangeArtifact(
            workspace_ref=ws.path,
            thread_id="t1",
            base_revision=ws.base_revision,
            changed_files=["calculator.py"],
            patch="diff",
            final_response="ok",
            source_node="codex_implementer",
        ),
        producer_node_id="codex_implementer",
        task_id="codex_tiny_repo",
    )
    limits = RuntimeLimits(
        max_parallel_benchmark_tasks=1,
        max_parallel_nodes_per_task=2,
        max_parallel_llm_calls=2,
        max_parallel_sandboxes=1,
    )
    executor = RepositoryTestHarnessExecutor()
    node = HarnessNodeSpec(
        node_id="repository_tests",
        harness_id="repository_test_harness",
        command=["python", "-m", "pytest", "-q"],
        input_slots={"repository_change": "RepositoryChangeArtifact"},
        output_slots={"result": "RepositoryHarnessResultArtifact"},
        timeout_seconds=60,
    )
    result = await executor.execute(
        node,
        {"repository_change": change},
        RunContext(
            run_id="run",
            task_id="codex_tiny_repo",
            run_dir=tmp_path,
            limits=limits,
            semaphores=RuntimeSemaphores(limits),
            contract_hash="c",
            workspace_ref=ws.path,
            subtask_id="implement_fix",
        ),
    )
    assert result.succeeded
    assert result.outputs["result"].payload["passed"] is True


@pytest.mark.asyncio
async def test_workspace_and_session_persisted_in_task_checkpoint(tmp_path):
    contracts = load_contracts(CONTRACTS)
    limits = RuntimeLimits(
        max_parallel_benchmark_tasks=1,
        max_parallel_nodes_per_task=2,
        max_parallel_llm_calls=2,
        max_parallel_sandboxes=1,
    )
    store = FileArtifactStore(tmp_path)
    registry = AgentBackendRegistry()
    registry.register(CodexSDKBackend(client_factory=lambda: _FakeCodex()))
    runtime = NativeAsyncRuntime(
        executors=NodeExecutorRegistry(
            agent_executor=AgentNodeExecutor(contracts, registry),
            harness_executor=HarnessNodeExecutor(MockSandbox()),
        ),
        artifact_store=store,
        checkpoint_store=CheckpointStore(tmp_path),
        event_writer=AppendOnlyEventWriter(tmp_path),
    )
    plan = TaskPlan.model_validate(
        yaml.safe_load(
            Path("configs/plans/codex_tiny_repo_single_subtask.yaml").read_text(
                encoding="utf-8"
            )
        )
    )
    build_compiler(CONTRACTS).compile(load_graph(plan.subtasks[0].local_graph_template))
    runner = SingleSubtaskCompatibilityRunner(
        runtime=runtime,
        artifact_store=store,
        task_checkpoint_store=TaskCheckpointStore(tmp_path),
        contracts_dir=CONTRACTS,
        source_repo=str(FIXTURE),
    )
    problem = ProblemArtifact(
        question_id="codex_tiny_repo",
        title="fix",
        statement="Fix tests",
        difficulty="easy",
        platform="fixture",
    )
    initial = create_artifact(
        problem, producer_node_id="__input__", task_id="codex_tiny_repo"
    )
    context = RunContext(
        run_id="run",
        task_id="codex_tiny_repo",
        run_dir=tmp_path,
        limits=limits,
        semaphores=RuntimeSemaphores(limits),
        contract_hash="contracts",
    )
    state, result = await runner.run(
        plan=plan,
        initial_artifacts=ArtifactBundle(slots={"problem": initial}),
        context=context,
    )
    assert result is not None
    assert state.frozen
    sub = state.subtasks["implement_fix"]
    assert sub.status is SubtaskStatus.COMMITTED
    assert sub.workspace_ref
    assert "codex_sdk" in sub.backend_sessions
    assert isinstance(sub.backend_sessions["codex_sdk"], BackendSessionRef)

    state2, result2 = await runner.run(
        plan=plan,
        initial_artifacts=ArtifactBundle(slots={"problem": initial}),
        context=context,
    )
    assert result2 is None
    assert state2.frozen
    loaded = await TaskCheckpointStore(tmp_path).load("codex_tiny_repo")
    assert loaded is not None
    assert loaded.subtasks["implement_fix"].workspace_ref
    assert loaded.subtasks["implement_fix"].backend_sessions["codex_sdk"].session_id


def test_codex_graph_compiles():
    compiled = build_compiler(CONTRACTS).compile(
        load_graph("configs/graphs/codex_single_implementer.yaml")
    )
    assert compiled.final_producers
