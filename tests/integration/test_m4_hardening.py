"""M4 final hardening integration coverage."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from orchestra.backends.codex_sdk import CodexSDKBackend
from orchestra.backends.registry import AgentBackendRegistry
from orchestra.control.fast_loop.schemas import FastLoopBudget, WorkspaceChangeSet
from orchestra.control.fast_loop.workspace import GitCandidateWorkspaceManager
from orchestra.control.ready_scheduler import ReadySubtaskScheduler
from orchestra.control.task_state import (
    SubtaskFailureReason,
    SubtaskStatus,
    TaskExecutionState,
)
from orchestra.decomposition.schemas import TaskPlan
from orchestra.executors.agent import AgentNodeExecutor
from orchestra.executors.harness import HarnessNodeExecutor
from orchestra.executors.registry import NodeExecutorRegistry
from orchestra.harness.command_runner import run_authoritative_harness_command
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


class _FakeTurn:
    final_response = "ok"
    usage = SimpleNamespace(input_tokens=1, output_tokens=1)


class _GoodThread:
    def __init__(self, workspace: str, tid: str) -> None:
        self.workspace = workspace
        self.id = tid

    async def run(self, prompt: str, **kwargs):  # noqa: ANN003
        del prompt, kwargs
        Path(self.workspace, "calculator.py").write_text(
            "def add(a, b):\n    return a + b\n", encoding="utf-8"
        )
        # New untracked helper used by tests conceptually.
        Path(self.workspace, "helper_note.txt").write_text("tracked via commit\n", encoding="utf-8")
        return _FakeTurn()


class _GoodCodex:
    def __init__(self) -> None:
        self.n = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):  # noqa: ANN002
        return None

    async def thread_start(self, **kwargs):  # noqa: ANN003
        self.n += 1
        return _GoodThread(kwargs["cwd"], f"t-{self.n}")


class _ConditionalThread:
    def __init__(self, workspace: str, tid: str) -> None:
        self.workspace = workspace
        self.id = tid

    async def run(self, prompt: str, **kwargs):  # noqa: ANN003
        del kwargs
        if "Previous attempt failed" in prompt or "pytest" in prompt or "FAILED" in prompt:
            Path(self.workspace, "calculator.py").write_text(
                "def add(a, b):\n    return a + b\n", encoding="utf-8"
            )
            Path(self.workspace, "notes_untracked.txt").write_text(
                "from candidate\n", encoding="utf-8"
            )
        else:
            Path(self.workspace, "calculator.py").write_text(
                "def add(a, b):\n    return a * b\n", encoding="utf-8"
            )
        return _FakeTurn()


class _CountingCodex:
    def __init__(self) -> None:
        self.threads: list[str] = []
        self._n = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):  # noqa: ANN002
        return None

    async def thread_start(self, **kwargs):  # noqa: ANN003
        self._n += 1
        tid = f"thread-{self._n}"
        self.threads.append(tid)
        return _ConditionalThread(kwargs["cwd"], tid)


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


def _ctx(tmp_path: Path, run_id: str, task_id: str = "codex_tiny_repo") -> RunContext:
    limits = RuntimeLimits(
        max_parallel_benchmark_tasks=1,
        max_parallel_nodes_per_task=2,
        max_parallel_llm_calls=2,
        max_parallel_sandboxes=2,
    )
    return RunContext(
        run_id=run_id,
        task_id=task_id,
        run_dir=tmp_path,
        limits=limits,
        semaphores=RuntimeSemaphores(limits),
        contract_hash="contracts",
    )


def _bundle(task_id: str = "codex_tiny_repo") -> ArtifactBundle:
    problem = ProblemArtifact(
        question_id=task_id,
        title="fix",
        statement="Fix calculator",
        difficulty="easy",
        platform="fixture",
    )
    return ArtifactBundle(
        slots={
            "problem": create_artifact(
                problem, producer_node_id="__input__", task_id=task_id
            )
        }
    )


@pytest.mark.asyncio
async def test_winner_commit_preserves_tracked_and_untracked_files(tmp_path):
    mgr = GitCandidateWorkspaceManager()
    base = await mgr.prepare_base_snapshot(
        source_repo=str(FIXTURE),
        run_dir=str(tmp_path),
        task_id="t",
        subtask_id="s",
    )
    winner = await mgr.fork_candidate_workspace(
        base=base,
        run_dir=str(tmp_path),
        task_id="t",
        subtask_id="s",
        candidate_id="w",
    )
    Path(winner.path, "calculator.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8"
    )
    Path(winner.path, "new_untracked_test.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8"
    )
    cs = await mgr.collect_changeset(winner)
    assert "calculator.py" in cs.modified_files
    assert "new_untracked_test.py" in cs.added_untracked_files
    committed = await mgr.commit_winner(
        base=base,
        winner=winner,
        expected_base_revision=base.base_revision,
        change_set=cs,
    )
    assert "return a + b" in Path(committed.path, "calculator.py").read_text(
        encoding="utf-8"
    )
    assert Path(committed.path, "new_untracked_test.py").is_file()
    passed, code, _, _ = await run_authoritative_harness_command(
        cwd=committed.path,
        command=["python", "-m", "pytest", "-q"],
        timeout_seconds=30,
    )
    assert passed is True
    assert code == 0


@pytest.mark.asyncio
async def test_failed_post_apply_harness_rolls_back_canonical_workspace(tmp_path):
    mgr = GitCandidateWorkspaceManager()
    base = await mgr.prepare_base_snapshot(
        source_repo=str(FIXTURE),
        run_dir=str(tmp_path),
        task_id="t",
        subtask_id="s",
    )
    expected = base.base_revision
    original = Path(base.path, "calculator.py").read_text(encoding="utf-8")
    winner = await mgr.fork_candidate_workspace(
        base=base,
        run_dir=str(tmp_path),
        task_id="t",
        subtask_id="s",
        candidate_id="bad",
    )
    # Apply a change that fails tests.
    Path(winner.path, "calculator.py").write_text(
        "def add(a, b):\n    return a * b\n", encoding="utf-8"
    )
    cs = await mgr.collect_changeset(winner)
    await mgr.apply_changeset(
        base=base,
        winner=winner,
        change_set=cs,
        expected_base_revision=expected,
    )
    passed, _, _, _ = await run_authoritative_harness_command(
        cwd=base.path,
        command=["python", "-m", "pytest", "-q"],
        timeout_seconds=30,
    )
    assert passed is False
    await mgr.rollback_to_revision(base, expected)
    assert Path(base.path, "calculator.py").read_text(encoding="utf-8") == original
    status = __import__("subprocess").run(
        ["git", "status", "--porcelain"],
        cwd=base.path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert status.stdout.strip() == ""


@pytest.mark.asyncio
async def test_canonical_harness_reruns_after_winner_apply(tmp_path):
    pytest.importorskip("openai_codex")
    client = _CountingCodex()
    registry = AgentBackendRegistry()
    registry.register(CodexSDKBackend(client_factory=lambda: client))
    runtime = _runtime(tmp_path, registry)
    plan = TaskPlan.model_validate(yaml.safe_load(PLAN.read_text(encoding="utf-8")))
    scheduler = ReadySubtaskScheduler(
        runtime=runtime,
        artifact_store=runtime.artifact_store,
        task_checkpoint_store=TaskCheckpointStore(tmp_path),
        contracts_dir=CONTRACTS,
        source_repo=str(FIXTURE),
        budget=FastLoopBudget(max_candidates=2, max_total_backend_calls=8),
    )
    state = TaskExecutionState.from_plan(plan)
    state = await scheduler.run_task(
        plan,
        state,
        initial_artifacts=_bundle(),
        context=_ctx(tmp_path, "harden-canonical"),
        source_repo=str(FIXTURE),
    )
    assert state.subtasks["implement_fix"].status is SubtaskStatus.COMMITTED
    assert state.canonical_workspace_ref
    passed, _, _, _ = await run_authoritative_harness_command(
        cwd=state.canonical_workspace_ref,
        command=["python", "-m", "pytest", "-q"],
        timeout_seconds=30,
    )
    assert passed is True


class _PropagationCodex:
    """Writes a unique file each call so downstream stages always have a diff."""

    def __init__(self) -> None:
        self._n = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):  # noqa: ANN002
        return None

    async def thread_start(self, **kwargs):  # noqa: ANN003
        self._n += 1
        return _SiblingFileThread(kwargs["cwd"], f"t-{self._n}", f"p{self._n}")


@pytest.mark.asyncio
async def test_downstream_subtask_receives_upstream_repository_changes(tmp_path):
    pytest.importorskip("openai_codex")
    client = _PropagationCodex()
    registry = AgentBackendRegistry()
    registry.register(CodexSDKBackend(client_factory=lambda: client))
    runtime = _runtime(tmp_path, registry)
    plan = TaskPlan.model_validate(
        {
            "task_id": "prop_repo",
            "plan_version": 1,
            "decomposition_rationale": "propagation",
            "decomposition_status": "disabled",
            "subtasks": [
                {
                    "subtask_id": "s1",
                    "title": "fix",
                    "objective": "fix calc",
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
                    "title": "extend",
                    "objective": "see s1",
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
    scheduler = ReadySubtaskScheduler(
        runtime=runtime,
        artifact_store=runtime.artifact_store,
        task_checkpoint_store=TaskCheckpointStore(tmp_path),
        contracts_dir=CONTRACTS,
        source_repo=str(FIXTURE),
        budget=FastLoopBudget(max_candidates=2, max_total_backend_calls=12),
    )
    state = TaskExecutionState.from_plan(plan)
    # Run only S1 first by temporarily making S2 pending with fake dep block —
    # scheduler will run S1 then S2.
    state = await scheduler.run_task(
        plan,
        state,
        initial_artifacts=_bundle("prop_repo"),
        context=_ctx(tmp_path, "prop", task_id="prop_repo"),
        source_repo=str(FIXTURE),
    )
    assert state.subtasks["s1"].status is SubtaskStatus.COMMITTED
    assert state.canonical_revision
    # Canonical must contain S1 fix.
    calc = Path(state.canonical_workspace_ref, "calculator.py").read_text(
        encoding="utf-8"
    )
    assert "return a + b" in calc
    assert state.subtasks["s2"].status is SubtaskStatus.COMMITTED
    # S2 workspace lineage records upstream revision.
    assert state.subtasks["s2"].base_task_revision


@pytest.mark.asyncio
async def test_uncommitted_candidate_changes_do_not_propagate(tmp_path):
    mgr = GitCandidateWorkspaceManager()
    base = await mgr.prepare_base_snapshot(
        source_repo=str(FIXTURE),
        run_dir=str(tmp_path),
        task_id="iso",
        subtask_id="s1",
    )
    loser = await mgr.fork_candidate_workspace(
        base=base,
        run_dir=str(tmp_path),
        task_id="iso",
        subtask_id="s1",
        candidate_id="loser",
    )
    Path(loser.path, "calculator.py").write_text(
        "def add(a, b):\n    return 999\n", encoding="utf-8"
    )
    # Base / canonical unchanged.
    assert "999" not in Path(base.path, "calculator.py").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_parallel_ready_subtasks_do_not_lose_checkpoint_state(tmp_path):
    pytest.importorskip("openai_codex")
    client = _GoodCodex()
    registry = AgentBackendRegistry()
    registry.register(CodexSDKBackend(client_factory=lambda: client))
    runtime = _runtime(tmp_path, registry)
    plan = TaskPlan.model_validate(
        {
            "task_id": "par",
            "plan_version": 1,
            "decomposition_rationale": "parallel",
            "decomposition_status": "disabled",
            "subtasks": [
                {
                    "subtask_id": "a",
                    "title": "a",
                    "objective": "a",
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
                    "subtask_id": "b",
                    "title": "b",
                    "objective": "b",
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
            ],
            "final_aggregation": {
                "strategy": "identity",
                "terminal_subtask_id": "b",
            },
            "communication_plan": {"version": 1},
        }
    )
    versions = []
    for i in range(3):
        run_dir = tmp_path / f"run-{i}"
        run_dir.mkdir(parents=True, exist_ok=True)
        store = TaskCheckpointStore(run_dir)
        scheduler = ReadySubtaskScheduler(
            runtime=runtime,
            artifact_store=runtime.artifact_store,
            task_checkpoint_store=store,
            contracts_dir=CONTRACTS,
            source_repo=str(FIXTURE),
            max_concurrent_subtasks=2,
            allow_concurrent_subtasks=True,
            budget=FastLoopBudget(max_candidates=1, max_total_backend_calls=8),
        )
        state = TaskExecutionState.from_plan(plan)
        state = await scheduler.run_task(
            plan,
            state,
            initial_artifacts=_bundle("par"),
            context=_ctx(run_dir, f"par-{i}", task_id="par"),
            source_repo=str(FIXTURE),
        )
        assert state.subtasks["a"].status is SubtaskStatus.COMMITTED
        assert state.subtasks["b"].status is SubtaskStatus.COMMITTED
        versions.append(state.state_version)
        loaded = await store.load("par")
        assert loaded is not None
        assert loaded.subtasks["a"].status is SubtaskStatus.COMMITTED
        assert loaded.subtasks["b"].status is SubtaskStatus.COMMITTED
    assert all(v >= 1 for v in versions)


@pytest.mark.asyncio
async def test_checkpoint_state_version_increases_monotonically(tmp_path):
    store = TaskCheckpointStore(tmp_path)
    plan = TaskPlan.model_validate(yaml.safe_load(PLAN.read_text(encoding="utf-8")))
    state = TaskExecutionState.from_plan(plan)
    assert state.state_version == 0
    state.state_version += 1
    await store.save(state)
    state.state_version += 1
    await store.save(state)
    loaded = await store.load("codex_tiny_repo")
    assert loaded is not None
    assert loaded.state_version == 2


@pytest.mark.asyncio
async def test_winner_commit_is_idempotent(tmp_path):
    mgr = GitCandidateWorkspaceManager()
    base = await mgr.prepare_base_snapshot(
        source_repo=str(FIXTURE),
        run_dir=str(tmp_path),
        task_id="t",
        subtask_id="s",
    )
    winner = await mgr.fork_candidate_workspace(
        base=base,
        run_dir=str(tmp_path),
        task_id="t",
        subtask_id="s",
        candidate_id="w",
    )
    Path(winner.path, "calculator.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8"
    )
    cs = await mgr.collect_changeset(winner)
    c1 = await mgr.commit_winner(
        base=base,
        winner=winner,
        expected_base_revision=base.base_revision,
        change_set=cs,
    )
    # Re-finalize with empty changeset must not corrupt canonical.
    c2 = await mgr.commit_winner(
        base=c1,
        winner=winner,
        expected_base_revision=c1.base_revision,
        change_set=WorkspaceChangeSet(),
    )
    assert Path(c2.path, "calculator.py").exists()
    assert "return a + b" in Path(c2.path, "calculator.py").read_text(encoding="utf-8")


class _SiblingFileThread:
    """Each sibling writes calculator fix + a unique file named by prompt tag."""

    def __init__(self, workspace: str, tid: str, tag: str) -> None:
        self.workspace = workspace
        self.id = tid
        self.tag = tag

    async def run(self, prompt: str, **kwargs):  # noqa: ANN003
        del prompt, kwargs
        Path(self.workspace, "calculator.py").write_text(
            "def add(a, b):\n    return a + b\n", encoding="utf-8"
        )
        Path(self.workspace, f"file_{self.tag}.py").write_text(
            f"{self.tag} = 1\n", encoding="utf-8"
        )
        return _FakeTurn()


class _SiblingCodex:
    def __init__(self) -> None:
        self._n = 0
        # Include a third tag so join-subtask s3 still produces a non-empty diff
        # after s1/s2 files are already present in the forked workspace.
        self._tags = ["a", "b", "c"]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):  # noqa: ANN002
        return None

    async def thread_start(self, **kwargs):  # noqa: ANN003
        tag = self._tags[min(self._n, len(self._tags) - 1)]
        self._n += 1
        return _SiblingFileThread(kwargs["cwd"], f"t-{self._n}", tag)


class _ConflictThread:
    def __init__(self, workspace: str, tid: str, value: int) -> None:
        self.workspace = workspace
        self.id = tid
        self.value = value

    async def run(self, prompt: str, **kwargs):  # noqa: ANN003
        del prompt, kwargs
        Path(self.workspace, "calculator.py").write_text(
            f"def add(a, b):\n    return {self.value}\n", encoding="utf-8"
        )
        return _FakeTurn()


class _ConflictCodex:
    """Alternating conflict writers: first +5 (pass), second 999 (different edit)."""

    def __init__(self) -> None:
        self._n = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):  # noqa: ANN002
        return None

    async def thread_start(self, **kwargs):  # noqa: ANN003
        self._n += 1
        # First returns 5 (passes tests); second returns 999 (conflicts after first commit).
        value = 5 if self._n == 1 else 999
        return _ConflictThread(kwargs["cwd"], f"t-{self._n}", value)


def _parallel_plan(task_id: str = "sib") -> TaskPlan:
    return TaskPlan.model_validate(
        {
            "task_id": task_id,
            "plan_version": 1,
            "decomposition_rationale": "siblings",
            "decomposition_status": "disabled",
            "subtasks": [
                {
                    "subtask_id": "s1",
                    "title": "s1",
                    "objective": "add file_a",
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
                    "title": "s2",
                    "objective": "add file_b",
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
                    "subtask_id": "s3",
                    "title": "s3",
                    "objective": "see both",
                    "dependencies": ["s1", "s2"],
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
                "terminal_subtask_id": "s3",
            },
            "communication_plan": {"version": 1},
        }
    )


@pytest.mark.asyncio
async def test_parallel_sibling_repository_commits_preserve_both_changes(tmp_path):
    pytest.importorskip("openai_codex")
    client = _SiblingCodex()
    registry = AgentBackendRegistry()
    registry.register(CodexSDKBackend(client_factory=lambda: client))
    runtime = _runtime(tmp_path, registry)
    plan = _parallel_plan("sib_files")
    store = TaskCheckpointStore(tmp_path)
    scheduler = ReadySubtaskScheduler(
        runtime=runtime,
        artifact_store=runtime.artifact_store,
        task_checkpoint_store=store,
        contracts_dir=CONTRACTS,
        source_repo=str(FIXTURE),
        max_concurrent_subtasks=2,
        allow_concurrent_subtasks=True,
        budget=FastLoopBudget(max_candidates=1, max_total_backend_calls=12),
    )
    state = TaskExecutionState.from_plan(plan)
    state = await scheduler.run_task(
        plan,
        state,
        initial_artifacts=_bundle("sib_files"),
        context=_ctx(tmp_path, "sib", task_id="sib_files"),
        source_repo=str(FIXTURE),
    )
    assert state.subtasks["s1"].status is SubtaskStatus.COMMITTED
    assert state.subtasks["s2"].status is SubtaskStatus.COMMITTED
    assert state.canonical_workspace_ref
    assert Path(state.canonical_workspace_ref, "file_a.py").is_file()
    assert Path(state.canonical_workspace_ref, "file_b.py").is_file()
    committed = [
        r
        for r in state.workspace_commit_records
        if r.status.value == "committed"
    ]
    assert len(committed) >= 2
    # Downstream S3 workspace sees both files via canonical fork.
    assert state.subtasks["s3"].status is SubtaskStatus.COMMITTED
    assert state.subtasks["s3"].base_task_revision


@pytest.mark.asyncio
async def test_parallel_sibling_same_line_conflict_fail_closed(tmp_path):
    """Direct coordinator-path conflict: one commit wins, other conflicts."""
    from orchestra.control.canonical_workspace import (
        CanonicalCommitError,
        CanonicalTaskWorkspaceManager,
    )

    mgr = GitCandidateWorkspaceManager()
    canonical_mgr = CanonicalTaskWorkspaceManager(mgr)
    can = await canonical_mgr.prepare(
        source_repo=str(FIXTURE), run_dir=str(tmp_path), task_id="cline"
    )
    w1 = await canonical_mgr.fork_subtask_workspace(
        canonical=can, run_dir=str(tmp_path), task_id="cline", subtask_id="s1"
    )
    w2 = await canonical_mgr.fork_subtask_workspace(
        canonical=can, run_dir=str(tmp_path), task_id="cline", subtask_id="s2"
    )
    Path(w1.path, "calculator.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8"
    )
    Path(w2.path, "calculator.py").write_text(
        "def add(a, b):\n    return a + b + 0\n", encoding="utf-8"
    )
    cs1 = await mgr.collect_changeset(w1)
    cs2 = await mgr.collect_changeset(w2)
    can, _ = await canonical_mgr.transactional_commit(
        canonical=can,
        winner_workspace=w1,
        change_set=cs1,
        run_dir=str(tmp_path),
        task_id="cline",
        subtask_id="s1",
        attempt_id=1,
        commit_message="s1",
        harness_command=["python", "-m", "pytest", "-q"],
        harness_timeout=30,
    )
    with pytest.raises(CanonicalCommitError) as exc:
        await canonical_mgr.transactional_commit(
            canonical=can,
            winner_workspace=w2,
            change_set=cs2,
            run_dir=str(tmp_path),
            task_id="cline",
            subtask_id="s2",
            attempt_id=1,
            commit_message="s2",
            harness_command=["python", "-m", "pytest", "-q"],
            harness_timeout=30,
        )
    assert exc.value.conflict is True
    text = Path(can.path, "calculator.py").read_text(encoding="utf-8")
    assert "return a + b\n" in text
    assert "a + b + 0" not in text


@pytest.mark.asyncio
async def test_uncommitted_candidate_artifacts_do_not_propagate(tmp_path):
    from orchestra.backends.base import ArtifactRef
    from orchestra.control.input_assembler import SubtaskInputAssembler
    from orchestra.schemas.artifacts import RepositoryChangeArtifact

    store = FileArtifactStore(tmp_path)
    assembler = SubtaskInputAssembler(store)
    problem = create_artifact(
        ProblemArtifact(
            question_id="u",
            title="u",
            statement="u",
            difficulty="easy",
            platform="fixture",
        ),
        producer_node_id="__input__",
        task_id="u",
    )
    cand = create_artifact(
        RepositoryChangeArtifact(
            workspace_ref="ws",
            thread_id="t",
            changed_files=["x.py"],
            patch="x",
            final_response="cand",
            source_node="s1",
        ),
        producer_node_id="s1",
        task_id="u",
    )
    await store.put(problem)
    await store.put(cand)
    plan = TaskPlan.model_validate(
        {
            "task_id": "u",
            "plan_version": 1,
            "decomposition_rationale": "x",
            "decomposition_status": "disabled",
            "subtasks": [
                {
                    "subtask_id": "s1",
                    "title": "s1",
                    "objective": "s1",
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
                    "title": "s2",
                    "objective": "s2",
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
    state = TaskExecutionState.from_plan(plan)
    state.subtasks["s1"].status = SubtaskStatus.FAILED
    state.subtasks["s1"].failure_reason = SubtaskFailureReason.CANONICAL_MERGE_CONFLICT
    state.subtasks["s1"].candidate_artifacts = [
        ArtifactRef(
            slot="plan",
            artifact_id=cand.artifact_id,
            artifact_type="RepositoryChangeArtifact",
        )
    ]
    assembled = await assembler.assemble(
        task_plan=plan,
        task_state=state,
        subtask=plan.subtasks[1],
        root_artifacts=ArtifactBundle(slots={"problem": problem}),
    )
    assert "plan" not in assembled.slots
    # S2 stays PENDING because S1 never COMMITTED.
    assert state.subtasks["s2"].status is SubtaskStatus.PENDING
