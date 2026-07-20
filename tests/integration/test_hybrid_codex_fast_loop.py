"""Hybrid Codex Fast Loop integration: RESUME/FORK + mixed node directives."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from orchestra.backends.capabilities import BackendCapabilities, SessionPolicy
from orchestra.backends.codex_sdk import CodexSDKBackend
from orchestra.backends.registry import AgentBackendRegistry
from orchestra.control.fast_loop.controller import FastLoopController
from orchestra.control.fast_loop.schemas import (
    CandidateRejectionReason,
    CandidateStatus,
    CodexSessionMode,
    FastLoopBudget,
    FastLoopConfig,
    HybridCodexConfig,
    MissingParentPolicy,
)
from orchestra.control.ready_scheduler import ReadySubtaskScheduler
from orchestra.control.single_subtask import SingleSubtaskCompatibilityRunner
from orchestra.control.task_state import SubtaskStatus, TaskExecutionState
from orchestra.decomposition.schemas import TaskPlan
from orchestra.executors.agent import AgentNodeExecutor
from orchestra.executors.harness import HarnessNodeExecutor
from orchestra.executors.registry import NodeExecutorRegistry
from orchestra.ir.artifacts import ArtifactBundle, create_artifact
from orchestra.ir.contracts import load_contracts
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
PLAN = Path("configs/plans/codex_tiny_repo_single_subtask.yaml")
AGENT_NODE = "codex_implementer"


class _FakeTurn:
    final_response = "ok"
    usage = SimpleNamespace(input_tokens=3, output_tokens=2)


def _write_calc(workspace: str, body: str) -> None:
    Path(workspace, "calculator.py").write_text(body, encoding="utf-8")


class _HybridThread:
    def __init__(self, cwd: str, thread_id: str) -> None:
        self.cwd = cwd
        self.id = thread_id

    async def run(self, prompt: str, **kwargs):  # noqa: ANN003
        del kwargs
        if (
            "Previous attempt failed" in prompt
            or "Harness" in prompt
            or "pytest" in prompt
            or "alternative" in prompt.lower()
            or "Continue" in prompt
        ):
            _write_calc(self.cwd, "def add(a, b):\n    return a + b\n")
        else:
            _write_calc(self.cwd, "def add(a, b):\n    return a * b\n")
        return _FakeTurn()


class _HybridCodexClient:
    def __init__(self) -> None:
        self._n = 0
        self.threads: dict[str, _HybridThread] = {}
        self.lifecycle_calls: list[tuple[str, str | None]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):  # noqa: ANN001
        return None

    async def thread_start(self, **kwargs):  # noqa: ANN003
        self._n += 1
        tid = f"thread-{self._n}"
        thread = _HybridThread(kwargs["cwd"], tid)
        self.threads[tid] = thread
        self.lifecycle_calls.append(("start", None))
        return thread

    async def thread_resume(self, parent_thread_id: str, **kwargs):  # noqa: ANN003
        self.lifecycle_calls.append(("resume", parent_thread_id))
        parent = self.threads[parent_thread_id]
        return _HybridThread(kwargs["cwd"], parent.id)

    async def thread_fork(self, parent_thread_id: str, **kwargs):  # noqa: ANN003
        self._n += 1
        tid = f"thread-fork-{self._n}"
        thread = _HybridThread(kwargs["cwd"], tid)
        self.threads[tid] = thread
        self.lifecycle_calls.append(("fork", parent_thread_id))
        return thread


def _limits() -> RuntimeLimits:
    return RuntimeLimits(
        max_parallel_benchmark_tasks=1,
        max_parallel_nodes_per_task=2,
        max_parallel_llm_calls=2,
        max_parallel_sandboxes=2,
    )


def _runtime(tmp_path: Path, registry: AgentBackendRegistry) -> NativeAsyncRuntime:
    contracts = load_contracts(CONTRACTS)
    return NativeAsyncRuntime(
        executors=NodeExecutorRegistry(
            agent_executor=AgentNodeExecutor(contracts, registry),
            harness_executor=HarnessNodeExecutor(MockSandbox()),
        ),
        artifact_store=FileArtifactStore(tmp_path),
        checkpoint_store=CheckpointStore(tmp_path),
        event_writer=AppendOnlyEventWriter(tmp_path),
    )


def _problem_bundle(task_id: str = "codex_tiny_repo") -> ArtifactBundle:
    problem = ProblemArtifact(
        question_id=task_id,
        title="fix",
        statement="Fix calculator tests",
        difficulty="easy",
        platform="fixture",
    )
    art = create_artifact(problem, producer_node_id="__input__", task_id=task_id)
    return ArtifactBundle(slots={"problem": art})


def _context(tmp_path: Path, run_id: str, task_id: str = "codex_tiny_repo") -> RunContext:
    limits = _limits()
    return RunContext(
        run_id=run_id,
        task_id=task_id,
        run_dir=tmp_path,
        limits=limits,
        semaphores=RuntimeSemaphores(limits),
        contract_hash="contracts",
    )


def _hybrid_fast_loop_config() -> FastLoopConfig:
    return FastLoopConfig(
        codex_session_mode=CodexSessionMode.HYBRID,
        hybrid_codex=HybridCodexConfig(
            enable_resume=True,
            enable_fork=True,
            add_fresh_critic=True,
            missing_parent_policy=MissingParentPolicy.REJECT,
        ),
        budget=FastLoopBudget(max_candidates=3, max_total_backend_calls=10),
    )


def _fresh_only_caps() -> dict[str, BackendCapabilities]:
    return {
        "codex_sdk": BackendCapabilities(
            multi_step=True,
            repository_editing=True,
            supports_session_state=False,
            supported_session_policies=frozenset({SessionPolicy.FRESH}),
            supports_parallel_instances=True,
        )
    }


@pytest.mark.asyncio
async def test_resume_candidate_flow(tmp_path):
    pytest.importorskip("openai_codex")
    client = _HybridCodexClient()
    registry = AgentBackendRegistry()
    registry.register(CodexSDKBackend(client_factory=lambda: client))
    runtime = _runtime(tmp_path, registry)
    plan = TaskPlan.model_validate(yaml.safe_load(PLAN.read_text(encoding="utf-8")))
    runner = SingleSubtaskCompatibilityRunner(
        runtime=runtime,
        artifact_store=runtime.artifact_store,
        task_checkpoint_store=TaskCheckpointStore(tmp_path),
        contracts_dir=CONTRACTS,
        source_repo=str(FIXTURE),
    )
    state, _ = await runner.run(
        plan=plan,
        initial_artifacts=_problem_bundle(),
        context=_context(tmp_path, "hybrid-initial"),
        source_repo=str(FIXTURE),
    )
    assert state.subtasks["implement_fix"].status is SubtaskStatus.HARNESS_FAILED
    parent_sessions = state.subtasks["implement_fix"].backend_sessions
    assert parent_sessions
    parent_id = parent_sessions[0].session_ref.session_id

    controller = FastLoopController(
        runtime=runtime,
        artifact_store=runtime.artifact_store,
        task_checkpoint_store=TaskCheckpointStore(tmp_path),
        contracts_dir=CONTRACTS,
        fast_loop_config=_hybrid_fast_loop_config(),
    )
    state = await controller.run(
        state=state,
        subtask_id="implement_fix",
        context=_context(tmp_path, "hybrid-resume"),
        initial_artifacts=_problem_bundle(),
        source_repo=str(FIXTURE),
    )
    fl = state.fast_loop_states["implement_fix"]
    resume = next(c for c in fl.candidates if c.candidate_id == "cand_resume")
    assert resume.session_directives[AGENT_NODE].source_session_ref.session_id == parent_id
    assert any(call == ("resume", parent_id) for call in client.lifecycle_calls)
    assert resume.status is not CandidateStatus.REJECTED
    if fl.selected_candidate_id == "cand_resume":
        assert state.subtasks["implement_fix"].status is SubtaskStatus.COMMITTED


@pytest.mark.asyncio
async def test_fork_two_candidates_separate_workspaces(tmp_path):
    pytest.importorskip("openai_codex")
    client = _HybridCodexClient()
    registry = AgentBackendRegistry()
    registry.register(CodexSDKBackend(client_factory=lambda: client))
    runtime = _runtime(tmp_path, registry)
    plan = TaskPlan.model_validate(yaml.safe_load(PLAN.read_text(encoding="utf-8")))
    runner = SingleSubtaskCompatibilityRunner(
        runtime=runtime,
        artifact_store=runtime.artifact_store,
        task_checkpoint_store=TaskCheckpointStore(tmp_path),
        contracts_dir=CONTRACTS,
        source_repo=str(FIXTURE),
    )
    state, _ = await runner.run(
        plan=plan,
        initial_artifacts=_problem_bundle(),
        context=_context(tmp_path, "hybrid-fork-init"),
        source_repo=str(FIXTURE),
    )
    controller = FastLoopController(
        runtime=runtime,
        artifact_store=runtime.artifact_store,
        task_checkpoint_store=TaskCheckpointStore(tmp_path),
        contracts_dir=CONTRACTS,
        fast_loop_config=FastLoopConfig(
            codex_session_mode=CodexSessionMode.FORK_ONLY,
            hybrid_codex=HybridCodexConfig(enable_resume=False, enable_fork=True),
            budget=FastLoopBudget(max_candidates=2, max_total_backend_calls=8),
        ),
    )
    state = await controller.run(
        state=state,
        subtask_id="implement_fix",
        context=_context(tmp_path, "hybrid-fork-run"),
        initial_artifacts=_problem_bundle(),
        source_repo=str(FIXTURE),
    )
    fl = state.fast_loop_states["implement_fix"]
    fork_candidates = [c for c in fl.candidates if c.candidate_id == "cand_fork"]
    assert fork_candidates
    ran = [
        c
        for c in fork_candidates
        if c.workspace_ref is not None and c.status is not CandidateStatus.REJECTED
    ]
    if len(ran) >= 1:
        paths = {c.workspace_ref.path for c in ran if c.workspace_ref}
        assert len(paths) == len(ran)
    fork_calls = [c for c in client.lifecycle_calls if c[0] == "fork"]
    assert fork_calls


@pytest.mark.asyncio
async def test_hybrid_graph_mixed_policies(tmp_path):
    pytest.importorskip("openai_codex")
    client = _HybridCodexClient()
    registry = AgentBackendRegistry()
    registry.register(CodexSDKBackend(client_factory=lambda: client))
    runtime = _runtime(tmp_path, registry)
    plan = TaskPlan.model_validate(yaml.safe_load(PLAN.read_text(encoding="utf-8")))
    runner = SingleSubtaskCompatibilityRunner(
        runtime=runtime,
        artifact_store=runtime.artifact_store,
        task_checkpoint_store=TaskCheckpointStore(tmp_path),
        contracts_dir=CONTRACTS,
        source_repo=str(FIXTURE),
    )
    state, _ = await runner.run(
        plan=plan,
        initial_artifacts=_problem_bundle(),
        context=_context(tmp_path, "hybrid-mixed-init"),
        source_repo=str(FIXTURE),
    )
    controller = FastLoopController(
        runtime=runtime,
        artifact_store=runtime.artifact_store,
        task_checkpoint_store=TaskCheckpointStore(tmp_path),
        contracts_dir=CONTRACTS,
        fast_loop_config=_hybrid_fast_loop_config(),
    )
    state = await controller.run(
        state=state,
        subtask_id="implement_fix",
        context=_context(tmp_path, "hybrid-mixed-run"),
        initial_artifacts=_problem_bundle(),
        source_repo=str(FIXTURE),
    )
    fl = state.fast_loop_states["implement_fix"]
    critic = next(c for c in fl.candidates if c.candidate_id == "cand_critic_repair")
    policies = {d.policy for d in critic.session_directives.values()}
    assert critic.session_directives[AGENT_NODE].policy in {
        SessionPolicy.FORK,
        SessionPolicy.RESUME,
    }
    verifier_ids = [n for n in critic.session_directives if n.endswith("__verifier")]
    if verifier_ids:
        assert SessionPolicy.FRESH in policies


@pytest.mark.asyncio
async def test_unsupported_caps_reject_before_backend_call(tmp_path):
    pytest.importorskip("openai_codex")
    client = _HybridCodexClient()
    registry = AgentBackendRegistry()
    registry.register(CodexSDKBackend(client_factory=lambda: client))
    runtime = _runtime(tmp_path, registry)
    plan = TaskPlan.model_validate(yaml.safe_load(PLAN.read_text(encoding="utf-8")))
    runner = SingleSubtaskCompatibilityRunner(
        runtime=runtime,
        artifact_store=runtime.artifact_store,
        task_checkpoint_store=TaskCheckpointStore(tmp_path),
        contracts_dir=CONTRACTS,
        source_repo=str(FIXTURE),
    )
    state, _ = await runner.run(
        plan=plan,
        initial_artifacts=_problem_bundle(),
        context=_context(tmp_path, "hybrid-reject-init"),
        source_repo=str(FIXTURE),
    )
    before_calls = len(client.lifecycle_calls)
    controller = FastLoopController(
        runtime=runtime,
        artifact_store=runtime.artifact_store,
        task_checkpoint_store=TaskCheckpointStore(tmp_path),
        contracts_dir=CONTRACTS,
        fast_loop_config=_hybrid_fast_loop_config(),
        capabilities=_fresh_only_caps(),
    )
    state = await controller.run(
        state=state,
        subtask_id="implement_fix",
        context=_context(tmp_path, "hybrid-reject-run"),
        initial_artifacts=_problem_bundle(),
        source_repo=str(FIXTURE),
    )
    fl = state.fast_loop_states["implement_fix"]
    rejected = [
        c
        for c in fl.candidates
        if c.status is CandidateStatus.REJECTED
        and c.rejection_reason is CandidateRejectionReason.UNSUPPORTED_SESSION_POLICY
    ]
    assert rejected
    assert len(client.lifecycle_calls) == before_calls


@pytest.mark.asyncio
async def test_checkpoint_restart_sketch(tmp_path):
    pytest.importorskip("openai_codex")
    client = _HybridCodexClient()
    registry = AgentBackendRegistry()
    registry.register(CodexSDKBackend(client_factory=lambda: client))
    runtime = _runtime(tmp_path, registry)
    plan = TaskPlan.model_validate(yaml.safe_load(PLAN.read_text(encoding="utf-8")))
    ckpt = TaskCheckpointStore(tmp_path)
    scheduler = ReadySubtaskScheduler(
        runtime=runtime,
        artifact_store=runtime.artifact_store,
        task_checkpoint_store=ckpt,
        contracts_dir=CONTRACTS,
        source_repo=str(FIXTURE),
        fast_loop_config=_hybrid_fast_loop_config(),
    )
    state = TaskExecutionState.from_plan(plan)
    state = await scheduler.run_task(
        plan,
        state,
        initial_artifacts=_problem_bundle(),
        context=_context(tmp_path, "hybrid-ckpt"),
        source_repo=str(FIXTURE),
    )
    loaded = await ckpt.load("codex_tiny_repo")
    assert loaded is not None
    fl = loaded.fast_loop_states.get("implement_fix")
    assert fl is not None
    assert fl.candidates
    parent_refs = {
        d.source_session_ref.session_id
        for c in fl.candidates
        for d in c.session_directives.values()
        if d.source_session_ref is not None
    }
    lineage_ids = {
        r.session_ref.session_id
        for r in loaded.session_lineage_records or []
        if hasattr(r, "session_ref")
        or isinstance(r, dict)
    }
    if parent_refs and loaded.session_lineage_records:
        assert parent_refs.intersection(lineage_ids) or state.subtasks[
            "implement_fix"
        ].status is SubtaskStatus.COMMITTED
