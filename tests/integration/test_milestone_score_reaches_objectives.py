"""The graded harness score has to survive the whole way to the objective record.

Each piece of this was unit tested and the chain still had a break in it: the
harness produced a score, the artifact carried it, and the objective record read
somewhere else entirely. That gap only shows up when a real scheduler run is
followed all the way through, which is what this does -- with the fast loop off,
because that is the configuration every A/B run uses and the one where the break
was invisible.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from orchestra.backends.base import (
    AgentResult,
    AgentRunStatus,
    BackendHealth,
    BackendSessionRef,
)
from orchestra.backends.capabilities import BackendCapabilities, SessionPolicy
from orchestra.backends.registry import AgentBackendRegistry
from orchestra.control.fast_loop.objectives import milestone_objectives
from orchestra.control.fast_loop.schemas import FastLoopBudget
from orchestra.control.ready_scheduler import ReadySubtaskScheduler
from orchestra.control.task_state import SubtaskStatus, TaskExecutionState
from orchestra.decomposition.schemas import TaskPlan
from orchestra.executors.agent import AgentNodeExecutor
from orchestra.executors.harness import HarnessNodeExecutor
from orchestra.executors.registry import NodeExecutorRegistry
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


class _EditingBackend:
    """Edits the repository so the harness has something to grade (test-only)."""

    def __init__(self) -> None:
        self.calls = 0

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
        )

    async def healthcheck(self) -> BackendHealth:
        return BackendHealth(healthy=True, backend_id=self.backend_id)

    async def run(self, request, context):  # noqa: ANN001
        from orchestra.workspaces.base import WorkspaceRef

        self.calls += 1
        workspace = context.workspace_ref
        Path(workspace, "calculator.py").write_text(
            "def add(a, b):\n    return a + b\n", encoding="utf-8"
        )
        snap = await SharedSubtaskGitWorkspaceManager().snapshot(
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
                thread_id=f"edit-{self.calls}",
                base_revision=snap.base_revision,
                changed_files=snap.changed_files,
                patch=snap.patch,
                final_response="edited",
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
            usage=LLMUsage(prompt_tokens=100, completion_tokens=10),
            session_ref=BackendSessionRef(
                backend_id=self.backend_id, session_id=f"edit-{self.calls}"
            ),
            backend_metadata={"workspace_ref": workspace, "model_name": "gpt-5.4"},
        )


def _grading_harness(tmp_path: Path, *, score: float, stage: str, passing: bool) -> list[str]:
    """A harness that reports graded progress the way the real one does."""
    script = tmp_path / "grading_harness.py"
    payload = {
        "score": score,
        "level": "integration",
        "stages": [
            {"stage": "compile", "passed_units": 1, "total_units": 1, "weight": 0.15},
            {"stage": "imports", "passed_units": 8, "total_units": 10, "weight": 0.25},
        ],
        "furthest_stage": stage,
    }
    script.write_text(
        "import sys\n"
        f"print('ADAMAS_HARNESS_SCORE ' + {json.dumps(json.dumps(payload))})\n"
        f"sys.exit({0 if passing else 1})\n",
        encoding="utf-8",
    )
    return ["python", str(script)]


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


def _plan(tmp_path: Path, command: list[str]) -> TaskPlan:
    graph = load_graph("configs/graphs/codex_single_implementer.yaml")
    nodes = []
    for node in graph.nodes:
        if getattr(node, "node_kind", "") == "harness":
            nodes.append(node.model_copy(update={"command": command}))
        elif isinstance(node, AgentNodeSpec):
            nodes.append(
                node.model_copy(
                    update={
                        "backend": SmolagentsCodeBackendConfig(
                            max_steps=2, executor_type="local"
                        )
                    }
                )
            )
        else:
            nodes.append(node)
    graph = graph.model_copy(update={"nodes": nodes, "graph_id": "graded_harness"})
    path = tmp_path / "graph.yaml"
    path.write_text(
        yaml.safe_dump(graph.model_dump(mode="json"), sort_keys=False), encoding="utf-8"
    )
    data = yaml.safe_load(PLAN.read_text(encoding="utf-8"))
    data["task_id"] = "graded"
    data["subtasks"][0]["local_graph_template"] = str(path)
    return TaskPlan.model_validate(data)


def _context(tmp_path: Path) -> RunContext:
    limits = RuntimeLimits(
        max_parallel_benchmark_tasks=1,
        max_parallel_nodes_per_task=2,
        max_parallel_llm_calls=2,
        max_parallel_sandboxes=2,
    )
    return RunContext(
        run_id="graded-run",
        task_id="graded",
        run_dir=tmp_path,
        limits=limits,
        semaphores=RuntimeSemaphores(limits),
        contract_hash="c",
    )


def _bundle() -> ArtifactBundle:
    problem = create_artifact(
        ProblemArtifact(
            question_id="graded",
            title="fix",
            statement="Fix calculator tests",
            difficulty="easy",
            platform="fixture",
        ),
        producer_node_id="__input__",
        task_id="graded",
    )
    return ArtifactBundle(slots={"problem": problem})


async def _run(tmp_path: Path, *, passing: bool, score: float) -> TaskExecutionState:
    registry = AgentBackendRegistry()
    registry.register(_EditingBackend())
    runtime = _runtime(tmp_path, registry)
    command = _grading_harness(tmp_path, score=score, stage="imports", passing=passing)
    plan = _plan(tmp_path, command)
    scheduler = ReadySubtaskScheduler(
        runtime=runtime,
        artifact_store=runtime.artifact_store,
        task_checkpoint_store=TaskCheckpointStore(tmp_path),
        contracts_dir=CONTRACTS,
        source_repo=str(FIXTURE),
        # Off, exactly as the A/B configuration has it. This is the setting
        # under which the score used to go missing.
        budget=FastLoopBudget(max_candidates=0, max_total_backend_calls=0),
    )
    state = TaskExecutionState.from_plan(
        plan, artifact_store_ref=str(tmp_path / "artifacts")
    )
    return await scheduler.run_task(
        plan,
        state,
        initial_artifacts=_bundle(),
        context=_context(tmp_path),
        source_repo=str(FIXTURE),
    )


@pytest.mark.asyncio
async def test_a_failing_milestone_still_reports_how_far_it_got(tmp_path: Path) -> None:
    """The case the whole axis exists for: a failure that is not a total loss."""
    state = await _run(tmp_path, passing=False, score=0.55)

    rows = milestone_objectives(state)
    assert len(rows) == 1
    row = rows[0]

    assert row.gate_passed is False
    assert row.harness_score == 0.55
    assert row.furthest_stage == "imports"
    # Without the graded score this would be a flat zero, indistinguishable from
    # a milestone that produced nothing at all.
    assert row.effective_score == 0.55
    assert row.total_tokens > 0


@pytest.mark.asyncio
async def test_a_passing_milestone_records_both_the_gate_and_the_score(
    tmp_path: Path,
) -> None:
    state = await _run(tmp_path, passing=True, score=1.0)

    row = milestone_objectives(state)[0]

    assert state.subtasks["implement_fix"].status is SubtaskStatus.COMMITTED
    assert row.gate_passed is True
    assert row.harness_score == 1.0


@pytest.mark.asyncio
async def test_the_score_is_recorded_on_the_attempt_not_only_in_an_artifact(
    tmp_path: Path,
) -> None:
    """Where it is written matters: the objective record reads the attempt.

    It used to read fast-loop candidates, which do not exist when the loop is
    off, so the artifact could hold a perfectly good score that nothing read.
    """
    state = await _run(tmp_path, passing=False, score=0.42)

    attempt = state.subtasks["implement_fix"].attempts[-1]

    assert attempt.metadata["harness_score"] == 0.42
    assert attempt.metadata["furthest_stage"] == "imports"


def test_a_harness_that_reports_nothing_leaves_the_score_absent(tmp_path: Path) -> None:
    """Plain pytest prints no marker, and absence must not read as zero."""
    from orchestra.harness.progress import parse_progress

    assert parse_progress("2 passed in 0.1s") == (None, [], "")
