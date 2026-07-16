"""Session persistence: node/attempt-scoped BackendSessionRecord round-trip."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from orchestra.backends.base import BackendSessionRef
from orchestra.backends.codex_sdk import CodexSDKBackend
from orchestra.backends.registry import AgentBackendRegistry
from orchestra.cli.validate_graph import build_compiler
from orchestra.control.single_subtask import SingleSubtaskCompatibilityRunner
from orchestra.control.task_state import BackendSessionRecord, SubtaskStatus
from orchestra.decomposition.schemas import TaskPlan
from orchestra.executors.agent import AgentNodeExecutor
from orchestra.executors.harness import HarnessNodeExecutor
from orchestra.executors.registry import NodeExecutorRegistry
from orchestra.ir.artifacts import ArtifactBundle, create_artifact
from orchestra.ir.contracts import load_contracts
from orchestra.ir.graph import load_graph
from orchestra.runtime.backend import RunContext
from orchestra.runtime.checkpoint import CheckpointStore
from orchestra.runtime.limits import RuntimeLimits, RuntimeSemaphores
from orchestra.runtime.native_async import NativeAsyncRuntime
from orchestra.runtime.task_checkpoint import TaskCheckpointStore
from orchestra.sandbox.mock import MockSandbox
from orchestra.schemas.artifacts import ProblemArtifact
from orchestra.storage.artifacts import FileArtifactStore
from orchestra.storage.events import AppendOnlyEventWriter

FIXTURE = Path("tests/fixtures/codex_tiny_repo").resolve()
CONTRACTS = "configs/contracts"


class _FakeTurn:
    final_response = "fixed"
    usage = SimpleNamespace(input_tokens=1, output_tokens=1)


class _FakeThread:
    id = "thread-test-001"

    def __init__(self, workspace: str) -> None:
        self.workspace = workspace

    async def run(self, prompt: str, **kwargs):  # noqa: ANN003
        del prompt, kwargs
        Path(self.workspace, "calculator.py").write_text(
            "def add(a, b):\n    return a + b\n", encoding="utf-8"
        )
        return _FakeTurn()


class _FakeCodex:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):  # noqa: ANN001
        return None

    async def thread_start(self, **kwargs):  # noqa: ANN003
        return _FakeThread(kwargs["cwd"])


@pytest.mark.asyncio
async def test_session_record_persists_across_checkpoint(tmp_path):
    pytest.importorskip("openai_codex")
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
        run_id="run-session",
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
    assert state.frozen is True
    subtask = state.subtasks["implement_fix"]
    assert subtask.status is SubtaskStatus.COMMITTED
    assert len(subtask.backend_sessions) == 1
    record = subtask.backend_sessions[0]
    assert record.node_id == "codex_implementer"
    assert record.backend_id == "codex_sdk"
    assert record.attempt_id == 1
    assert record.candidate_id is None
    assert record.session_ref.session_id == "thread-test-001"

    loaded = await TaskCheckpointStore(tmp_path).load("codex_tiny_repo")
    assert loaded is not None
    loaded_record = loaded.subtasks["implement_fix"].backend_sessions[0]
    assert loaded_record.node_id == "codex_implementer"
    assert loaded_record.backend_id == "codex_sdk"
    assert loaded_record.attempt_id == 1
    assert loaded_record.candidate_id is None
    assert loaded_record.session_ref.session_id == "thread-test-001"


def test_two_nodes_same_backend_keep_distinct_session_records():
    records = [
        BackendSessionRecord(
            node_id="codex_coder",
            backend_id="codex_sdk",
            attempt_id=1,
            session_ref=BackendSessionRef(
                backend_id="codex_sdk", session_id="thread-coder"
            ),
        ),
        BackendSessionRecord(
            node_id="codex_reviewer",
            backend_id="codex_sdk",
            attempt_id=1,
            session_ref=BackendSessionRef(
                backend_id="codex_sdk", session_id="thread-reviewer"
            ),
        ),
    ]
    assert len(records) == 2
    by_node = {item.node_id: item.session_ref.session_id for item in records}
    assert by_node["codex_coder"] == "thread-coder"
    assert by_node["codex_reviewer"] == "thread-reviewer"
