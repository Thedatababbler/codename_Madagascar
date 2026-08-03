"""M4 Fast Loop integration: CodeAgent/Codex-compatible FRESH candidates."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from orchestra.backends.base import (
    AgentError,
    AgentRequest,
    AgentResult,
    AgentRunStatus,
    AgentSessionPolicy,
    BackendExecutionContext,
    BackendHealth,
    BackendSessionRef,
)
from orchestra.backends.capabilities import BackendCapabilities, SessionPolicy
from orchestra.backends.codex_sdk import CodexSDKBackend
from orchestra.backends.registry import AgentBackendRegistry
from orchestra.cli.validate_graph import build_compiler
from orchestra.control.fast_loop.controller import FastLoopController
from orchestra.control.fast_loop.schemas import CandidateStatus, FastLoopBudget
from orchestra.control.ready_scheduler import ReadySubtaskScheduler
from orchestra.control.single_subtask import SingleSubtaskCompatibilityRunner
from orchestra.control.task_state import SubtaskFailureReason, SubtaskStatus, TaskExecutionState
from orchestra.decomposition.schemas import TaskPlan
from orchestra.executors.agent import AgentNodeExecutor
from orchestra.executors.harness import HarnessNodeExecutor
from orchestra.executors.registry import NodeExecutorRegistry
from orchestra.harness.env_redaction import build_harness_env
from orchestra.harness.repository_test import TRUSTED_MARKER, workspace_is_trusted
from orchestra.ir.artifacts import ArtifactBundle, create_artifact
from orchestra.ir.contracts import load_contracts
from orchestra.ir.graph import load_graph
from orchestra.ir.nodes import AgentNodeSpec, SmolagentsCodeBackendConfig
from orchestra.llm.usage import LLMUsage
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
PLAN = Path("configs/plans/codex_tiny_repo_single_subtask.yaml")


class _FakeTurn:
    final_response = "ok"
    usage = SimpleNamespace(input_tokens=3, output_tokens=2)


def _write_calc(workspace: str, body: str) -> None:
    Path(workspace, "calculator.py").write_text(body, encoding="utf-8")


class _ConditionalThread:
    """Wrong patch unless prompt contains failure feedback."""

    def __init__(self, workspace: str, thread_id: str) -> None:
        self.workspace = workspace
        self.id = thread_id

    async def run(self, prompt: str, **kwargs):  # noqa: ANN003
        del kwargs
        if (
            "Previous attempt failed" in prompt
            or "Harness" in prompt
            or "pytest" in prompt
        ):
            _write_calc(self.workspace, "def add(a, b):\n    return a + b\n")
        else:
            _write_calc(self.workspace, "def add(a, b):\n    return a * b\n")
        return _FakeTurn()


class _CountingCodex:
    def __init__(self) -> None:
        self.threads: list[str] = []
        self._n = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):  # noqa: ANN001
        return None

    async def thread_start(self, **kwargs):  # noqa: ANN003
        self._n += 1
        tid = f"thread-{self._n}"
        self.threads.append(tid)
        return _ConditionalThread(kwargs["cwd"], tid)


class _FreshOnlyCodeAgentFake:
    """Fake CodeAgent backend that edits a repository workspace (test-only)."""

    def __init__(self) -> None:
        self.calls = 0
        self.session_policies: list[AgentSessionPolicy] = []

    @property
    def backend_id(self) -> str:
        return "smolagents_code"

    @property
    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            multi_step=True,
            code_actions=True,
            structured_tools=True,
            repository_editing=True,
            supports_session_state=False,
            supported_session_policies=frozenset({SessionPolicy.FRESH}),
            supports_workspace_rebinding=True,
            supports_parallel_instances=True,
            supports_tool_policy_edit=True,
            supports_model_override=True,
            supports_step_trace=True,
        )

    async def healthcheck(self) -> BackendHealth:
        return BackendHealth(healthy=True, backend_id=self.backend_id)

    async def run(
        self, request: AgentRequest, context: BackendExecutionContext
    ) -> AgentResult:
        self.calls += 1
        self.session_policies.append(request.session_policy)
        if request.session_policy is not AgentSessionPolicy.FRESH:
            return AgentResult(
                request_id=request.request_id,
                backend_id=self.backend_id,
                status=AgentRunStatus.INVALID_REQUEST,
                error=AgentError(
                    status=AgentRunStatus.INVALID_REQUEST,
                    message="CodeAgent supports FRESH only",
                ),
            )
        assert context.workspace_ref
        workspace = context.workspace_ref
        feedback = " ".join(m.get("content", "") for m in request.messages)
        if "Previous attempt failed" in feedback or self.calls > 1:
            _write_calc(workspace, "def add(a, b):\n    return a + b\n")
        else:
            _write_calc(workspace, "def add(a, b):\n    return a * b\n")
        mgr = SharedSubtaskGitWorkspaceManager()
        from orchestra.workspaces.base import WorkspaceRef

        snap = await mgr.snapshot(
            WorkspaceRef(
                workspace_id="cand",
                path=workspace,
                task_id=context.task_id,
                subtask_id=context.subtask_id or "main",
            )
        )
        artifact = create_artifact(
            RepositoryChangeArtifact(
                workspace_ref=workspace,
                thread_id=f"codeagent-{self.calls}",
                base_revision=snap.base_revision,
                changed_files=snap.changed_files,
                patch=snap.patch,
                final_response="fake codeagent edit",
                source_node=request.node_id,
            ),
            producer_node_id=request.node_id,
            task_id=context.task_id,
        )
        return AgentResult(
            request_id=request.request_id,
            backend_id=self.backend_id,
            status=AgentRunStatus.SUCCESS,
            output_artifacts=[artifact],
            usage=LLMUsage(prompt_tokens=1, completion_tokens=1),
            session_ref=BackendSessionRef(
                backend_id=self.backend_id,
                session_id=f"codeagent-{self.calls}",
            ),
            backend_metadata={"workspace_ref": workspace},
        )


class _InfraFailThenOkCodex:
    def __init__(self) -> None:
        self.n = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):  # noqa: ANN001
        return None

    async def thread_start(self, **kwargs):  # noqa: ANN003
        self.n += 1
        if self.n == 1:
            raise RuntimeError("simulated infra outage")

        class _T:
            id = "thread-infra-ok"

            def __init__(self, cwd: str) -> None:
                self.cwd = cwd

            async def run(self, prompt: str, **kw):  # noqa: ANN003
                del prompt, kw
                _write_calc(self.cwd, "def add(a, b):\n    return a + b\n")
                return _FakeTurn()

        return _T(kwargs["cwd"])


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


@pytest.mark.asyncio
async def test_a_codeagent_fast_loop_feedback_commit(tmp_path):
    fake = _FreshOnlyCodeAgentFake()
    registry = AgentBackendRegistry()
    registry.register(fake)
    graph = load_graph("configs/graphs/codex_single_implementer.yaml")
    nodes = []
    for node in graph.nodes:
        if isinstance(node, AgentNodeSpec):
            nodes.append(
                node.model_copy(
                    update={
                        "backend": SmolagentsCodeBackendConfig(
                            max_steps=4,
                            executor_type="local",
                        )
                    }
                )
            )
        else:
            nodes.append(node)
    graph = graph.model_copy(update={"nodes": nodes, "graph_id": "codeagent_impl"})
    graph_path = tmp_path / "codeagent_graph.yaml"
    graph_path.write_text(
        yaml.safe_dump(graph.model_dump(mode="json"), sort_keys=False),
        encoding="utf-8",
    )
    plan_data = yaml.safe_load(PLAN.read_text(encoding="utf-8"))
    plan_data["task_id"] = "codeagent_tiny"
    plan_data["subtasks"][0]["local_graph_template"] = str(graph_path)
    plan = TaskPlan.model_validate(plan_data)

    runtime = _runtime(tmp_path, registry)
    store = runtime.artifact_store
    ckpt = TaskCheckpointStore(tmp_path)
    scheduler = ReadySubtaskScheduler(
        runtime=runtime,
        artifact_store=store,
        task_checkpoint_store=ckpt,
        contracts_dir=CONTRACTS,
        source_repo=str(FIXTURE),
        budget=FastLoopBudget(max_candidates=2, max_total_backend_calls=6),
    )
    state = TaskExecutionState.from_plan(
        plan, artifact_store_ref=str(tmp_path / "artifacts")
    )
    state = await scheduler.run_task(
        plan,
        state,
        initial_artifacts=_problem_bundle("codeagent_tiny"),
        context=_context(tmp_path, "run-a", task_id="codeagent_tiny"),
        source_repo=str(FIXTURE),
    )
    sub = state.subtasks["implement_fix"]
    assert sub.status is SubtaskStatus.COMMITTED
    assert all(p is AgentSessionPolicy.FRESH for p in fake.session_policies)
    fl = state.fast_loop_states["implement_fix"]
    assert fl.selected_candidate_id is not None
    winner = next(
        c for c in fl.candidates if c.candidate_id == fl.selected_candidate_id
    )
    assert winner.status is CandidateStatus.COMMITTED
    calc = Path(sub.workspace_ref, "calculator.py").read_text(encoding="utf-8")
    assert "return a + b" in calc


@pytest.mark.asyncio
async def test_b_codex_fast_loop_two_fresh_sessions(tmp_path):
    pytest.importorskip("openai_codex")
    client = _CountingCodex()
    registry = AgentBackendRegistry()
    registry.register(CodexSDKBackend(client_factory=lambda: client))
    runtime = _runtime(tmp_path, registry)
    plan = TaskPlan.model_validate(yaml.safe_load(PLAN.read_text(encoding="utf-8")))
    build_compiler(CONTRACTS).compile(load_graph(plan.subtasks[0].local_graph_template))
    ckpt = TaskCheckpointStore(tmp_path)
    scheduler = ReadySubtaskScheduler(
        runtime=runtime,
        artifact_store=runtime.artifact_store,
        task_checkpoint_store=ckpt,
        contracts_dir=CONTRACTS,
        source_repo=str(FIXTURE),
        budget=FastLoopBudget(max_candidates=3, max_total_backend_calls=8),
    )
    state = TaskExecutionState.from_plan(plan)
    state = await scheduler.run_task(
        plan,
        state,
        initial_artifacts=_problem_bundle(),
        context=_context(tmp_path, "run-b"),
        source_repo=str(FIXTURE),
    )
    assert state.subtasks["implement_fix"].status is SubtaskStatus.COMMITTED
    assert len(set(client.threads)) >= 2
    fl = state.fast_loop_states.get("implement_fix")
    assert fl is not None
    cand_sessions = [
        s for c in fl.candidates for s in c.backend_sessions if s.candidate_id
    ]
    assert cand_sessions
    ids = {s.session_ref.session_id for s in cand_sessions}
    assert len(ids) == len(cand_sessions)


@pytest.mark.asyncio
async def test_c_parallel_workspace_isolation(tmp_path):
    from orchestra.control.fast_loop.workspace import GitCandidateWorkspaceManager

    mgr = GitCandidateWorkspaceManager()
    base = await mgr.prepare_base_snapshot(
        source_repo=str(FIXTURE),
        run_dir=str(tmp_path),
        task_id="iso",
        subtask_id="s",
    )

    async def _mutate(cid: str, payload: str) -> str:
        ws = await mgr.fork_candidate_workspace(
            base=base,
            run_dir=str(tmp_path),
            task_id="iso",
            subtask_id="s",
            candidate_id=cid,
        )
        Path(ws.path, "calculator.py").write_text(payload, encoding="utf-8")
        await asyncio.sleep(0.01)
        return Path(ws.path, "calculator.py").read_text(encoding="utf-8")

    a, b = await asyncio.gather(
        _mutate("a", "def add(a, b):\n    return 1\n"),
        _mutate("b", "def add(a, b):\n    return 2\n"),
    )
    assert "return 1" in a
    assert "return 2" in b
    base_text = Path(base.path, "calculator.py").read_text(encoding="utf-8")
    assert "return 1" not in base_text
    assert "return 2" not in base_text


@pytest.mark.asyncio
async def test_d_harness_secret_env_and_trusted_marker(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-secret")
    env = build_harness_env()
    assert "OPENAI_API_KEY" not in env
    untrusted = tmp_path / "untrusted"
    untrusted.mkdir()
    assert workspace_is_trusted(untrusted) is False
    (untrusted / TRUSTED_MARKER).write_text("ok\n", encoding="utf-8")
    assert workspace_is_trusted(untrusted) is True


@pytest.mark.asyncio
async def test_e_checkpoint_resume_skips_completed_candidate(tmp_path):
    pytest.importorskip("openai_codex")
    client = _CountingCodex()
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
        context=_context(tmp_path, "run-e-base"),
        source_repo=str(FIXTURE),
    )
    assert state.subtasks["implement_fix"].status is SubtaskStatus.HARNESS_FAILED

    controller = FastLoopController(
        runtime=runtime,
        artifact_store=runtime.artifact_store,
        task_checkpoint_store=TaskCheckpointStore(tmp_path),
        contracts_dir=CONTRACTS,
        budget=FastLoopBudget(max_candidates=2, max_total_backend_calls=6),
    )
    state = await controller.run(
        state=state,
        subtask_id="implement_fix",
        context=_context(tmp_path, "run-e-fl"),
        initial_artifacts=_problem_bundle(),
        source_repo=str(FIXTURE),
    )
    loaded = await TaskCheckpointStore(tmp_path).load("codex_tiny_repo")
    assert loaded is not None
    if loaded.subtasks["implement_fix"].status is SubtaskStatus.COMMITTED:
        fl = loaded.fast_loop_states["implement_fix"]
        assert fl.selected_candidate_id
        before = fl.selected_candidate_id
        again = await controller.run(
            state=loaded,
            subtask_id="implement_fix",
            context=_context(tmp_path, "run-e-resume"),
            initial_artifacts=_problem_bundle(),
            source_repo=str(FIXTURE),
        )
        assert again.fast_loop_states["implement_fix"].selected_candidate_id == before
        winners = [
            c
            for c in again.fast_loop_states["implement_fix"].candidates
            if c.status is CandidateStatus.COMMITTED
        ]
        assert len(winners) == 1


@pytest.mark.asyncio
async def test_f_infra_failure_no_prompt_edit(tmp_path):
    pytest.importorskip("openai_codex")
    client = _InfraFailThenOkCodex()
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
    # Codex maps infra exceptions into AgentResult (does not re-raise).
    state, _ = await runner.run(
        plan=plan,
        initial_artifacts=_problem_bundle(),
        context=_context(tmp_path, "run-f"),
        source_repo=str(FIXTURE),
    )
    sub = state.subtasks["implement_fix"]
    assert sub.failure_reason is SubtaskFailureReason.INFRA
    controller = FastLoopController(
        runtime=runtime,
        artifact_store=runtime.artifact_store,
        task_checkpoint_store=TaskCheckpointStore(tmp_path),
        contracts_dir=CONTRACTS,
    )
    state = await controller.run(
        state=state,
        subtask_id="implement_fix",
        context=_context(tmp_path, "run-f-fl"),
        initial_artifacts=_problem_bundle(),
        source_repo=str(FIXTURE),
    )
    fl = state.fast_loop_states["implement_fix"]
    assert fl.infra_retries_used == 1
    prompt_edits = [
        e
        for c in fl.candidates
        for e in c.edits
        if getattr(e, "type", None) == "prompt_feedback"
    ]
    assert prompt_edits == []
    # Infra retry may recover without treating the outage as graph-quality failure.
    assert fl.candidates[0].metadata.get("infrastructure_related") is True


class _AlwaysFixCodex:
    """Always writes a correct calculator plus a unique file (non-empty diffs)."""

    def __init__(self) -> None:
        self._n = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):  # noqa: ANN002
        return None

    async def thread_start(self, **kwargs):  # noqa: ANN003
        self._n += 1
        workspace = kwargs["cwd"]
        tid = f"thread-{self._n}"

        class _T:
            id = tid

            async def run(self, prompt: str, **kw):  # noqa: ANN003
                del prompt, kw
                _write_calc(workspace, "def add(a, b):\n    return a + b\n")
                Path(workspace, f"stage_{tid}.txt").write_text(f"{tid}\n", encoding="utf-8")
                return _FakeTurn()

        return _T()


@pytest.mark.asyncio
async def test_g_multi_subtask_scheduler_dag(tmp_path):
    pytest.importorskip("openai_codex")
    client = _AlwaysFixCodex()
    registry = AgentBackendRegistry()
    registry.register(CodexSDKBackend(client_factory=lambda: client))
    runtime = _runtime(tmp_path, registry)

    plan = TaskPlan.model_validate(
        {
            "task_id": "m4_multi",
            "plan_version": 1,
            "decomposition_rationale": "m4 scheduler test",
            "decomposition_status": "disabled",
            "subtasks": [
                {
                    "subtask_id": "s1",
                    "title": "first",
                    "objective": "fix",
                    "dependencies": [],
                    "keystone_harness_id": "repository_test_harness",
                    "local_graph_template": "configs/graphs/codex_single_implementer.yaml",
                    "budget": {
                        "max_llm_calls": 2,
                        "max_steps": 2,
                        "timeout_seconds": 120,
                    },
                    "input_artifacts": [
                        {
                            "slot": "problem",
                            "artifact_id": "",
                            "artifact_type": "ProblemArtifact",
                        }
                    ],
                    "expected_outputs": [
                        {
                            "parser_id": "repository_change",
                            "output_schema": "RepositoryChangeArtifact",
                        }
                    ],
                },
                {
                    "subtask_id": "s2",
                    "title": "second",
                    "objective": "noop after s1",
                    "dependencies": ["s1"],
                    "keystone_harness_id": "repository_test_harness",
                    "local_graph_template": "configs/graphs/codex_single_implementer.yaml",
                    "budget": {
                        "max_llm_calls": 2,
                        "max_steps": 2,
                        "timeout_seconds": 120,
                    },
                    "input_artifacts": [
                        {
                            "slot": "problem",
                            "artifact_id": "",
                            "artifact_type": "ProblemArtifact",
                        }
                    ],
                    "expected_outputs": [
                        {
                            "parser_id": "repository_change",
                            "output_schema": "RepositoryChangeArtifact",
                        }
                    ],
                },
            ],
            "final_aggregation": {
                "strategy": "identity",
                "terminal_subtask_id": "s2",
            },
            "communication_plan": {"version": 1},
        }
    )
    original_plan_hash = plan.content_hash()
    scheduler = ReadySubtaskScheduler(
        runtime=runtime,
        artifact_store=runtime.artifact_store,
        task_checkpoint_store=TaskCheckpointStore(tmp_path),
        contracts_dir=CONTRACTS,
        source_repo=str(FIXTURE),
        budget=FastLoopBudget(max_candidates=2, max_total_backend_calls=10),
    )
    state = TaskExecutionState.from_plan(plan)
    state = await scheduler.run_task(
        plan,
        state,
        initial_artifacts=_problem_bundle("m4_multi"),
        context=_context(tmp_path, "run-g", task_id="m4_multi"),
        source_repo=str(FIXTURE),
    )
    assert state.subtasks["s1"].status is SubtaskStatus.COMMITTED
    assert state.subtasks["s2"].status is SubtaskStatus.COMMITTED
    assert state.task_plan.content_hash() == original_plan_hash
