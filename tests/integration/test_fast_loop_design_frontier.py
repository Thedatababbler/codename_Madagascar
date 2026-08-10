"""A milestone's design search, followed all the way through a scheduler run.

The unit tests establish that the frontier is built correctly from candidate
records. What they cannot establish is that a real run produces candidates whose
axes differ at all -- and that is the property the whole experiment rests on. A
search whose candidates always land on the same point is a ranking with extra
steps, and it would look identical in every unit test.

So this drives the actual fast loop with a backend and a harness that give each
candidate a different score and a different cost, and asserts that two mutually
non-dominating designs survive to be recorded.
"""

from __future__ import annotations

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
from orchestra.control.fast_loop.pareto import ParetoSelectionConfig
from orchestra.control.fast_loop.schemas import FastLoopBudget
from orchestra.control.ready_scheduler import ReadySubtaskScheduler
from orchestra.control.task_state import TaskExecutionState
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

#: What each successive harness invocation reports: the original attempt, then
#: one per candidate. Chosen so exactly one pair trades quality against cost --
#: candidate 1 scores 0.5 cheaply, candidate 2 scores 0.9 dearly, and candidate 3
#: scores 0.5 dearly and is therefore dominated by candidate 1.
#:
#: The tail of the sequence passes because the commit re-runs the harness on the
#: canonical workspace: the winning change has to verify a second time, and a
#: script that failed that re-run would roll the commit back and leave nothing to
#: assert about which candidate was selected.
HARNESS_SCRIPT = """
import json, sys
from pathlib import Path

counter = Path(r"{counter}")
n = int(counter.read_text()) + 1 if counter.exists() else 1
counter.write_text(str(n))
scores = [0.3, 0.5, 0.9, 0.5]
passes = [False, False, True, True]
i = min(n, len(scores)) - 1
print("ADAMAS_HARNESS_SCORE " + json.dumps({{
    "score": scores[i],
    "level": "integration",
    "stages": [
        {{"stage": "compile", "passed_units": 1, "total_units": 1, "weight": 0.15}},
        {{"stage": "imports", "passed_units": 8, "total_units": 10, "weight": 0.25}},
    ],
    "furthest_stage": "imports",
}}))
sys.exit(0 if passes[i] else 1)
"""


class _CountingBackend:
    """Spends more on every call, so candidates differ on the cost axis.

    Candidates that all cost the same cannot produce a trade-off, and a frontier
    with nothing to trade is exactly the degenerate case this test exists to rule
    out.
    """

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
            f"def add(a, b):\n    return a + b  # call {self.calls}\n", encoding="utf-8"
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
            usage=LLMUsage(
                prompt_tokens=100_000 * self.calls, completion_tokens=1_000 * self.calls
            ),
            session_ref=BackendSessionRef(
                backend_id=self.backend_id, session_id=f"edit-{self.calls}"
            ),
            backend_metadata={"workspace_ref": workspace, "model_name": "gpt-5.4"},
        )


def _harness_command(tmp_path: Path) -> list[str]:
    script = tmp_path / "staged_harness.py"
    script.write_text(
        HARNESS_SCRIPT.format(counter=tmp_path / "harness_calls.txt"), encoding="utf-8"
    )
    return ["python", str(script)]


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
    graph = graph.model_copy(update={"nodes": nodes, "graph_id": "design_frontier"})
    path = tmp_path / "graph.yaml"
    path.write_text(
        yaml.safe_dump(graph.model_dump(mode="json"), sort_keys=False), encoding="utf-8"
    )
    data = yaml.safe_load(PLAN.read_text(encoding="utf-8"))
    data["task_id"] = "frontier"
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
        run_id="frontier-run",
        task_id="frontier",
        run_dir=tmp_path,
        limits=limits,
        semaphores=RuntimeSemaphores(limits),
        contract_hash="c",
    )


def _bundle() -> ArtifactBundle:
    problem = create_artifact(
        ProblemArtifact(
            question_id="frontier",
            title="fix",
            statement="Fix calculator tests",
            difficulty="easy",
            platform="fixture",
        ),
        producer_node_id="__input__",
        task_id="frontier",
    )
    return ArtifactBundle(slots={"problem": problem})


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


async def _run(tmp_path: Path, *, design_search: bool) -> TaskExecutionState:
    registry = AgentBackendRegistry()
    registry.register(_CountingBackend())
    runtime = _runtime(tmp_path, registry)
    plan = _plan(tmp_path, _harness_command(tmp_path))
    scheduler = ReadySubtaskScheduler(
        runtime=runtime,
        artifact_store=runtime.artifact_store,
        task_checkpoint_store=TaskCheckpointStore(tmp_path),
        contracts_dir=CONTRACTS,
        source_repo=str(FIXTURE),
        budget=FastLoopBudget(
            max_candidates=3,
            max_total_backend_calls=12,
            max_attempts_per_subtask=4,
            max_wall_time_seconds=600,
        ),
        design_search=design_search,
        pareto=ParetoSelectionConfig(),
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


def _fast_loop(state: TaskExecutionState):  # noqa: ANN202 - FastLoopState
    assert state.fast_loop_states, "the gate had to fail for a search to happen at all"
    return next(iter(state.fast_loop_states.values()))


@pytest.mark.asyncio
async def test_the_search_tries_more_than_one_design(tmp_path: Path) -> None:
    fast_loop = _fast_loop(await _run(tmp_path, design_search=True))
    executed = [c for c in fast_loop.candidates if c.cost.backend_calls]
    assert len(executed) >= 2
    # Distinct graphs, not distinct labels: candidates that compile to the same
    # graph would be duplicate points on the frontier.
    assert len({c.graph_hash for c in executed}) == len(executed)


@pytest.mark.asyncio
async def test_two_designs_that_trade_off_both_survive(tmp_path: Path) -> None:
    fast_loop = _fast_loop(await _run(tmp_path, design_search=True))
    scored = [c for c in fast_loop.candidates if c.harness_score is not None]
    assert len(scored) >= 2
    # The property the experiment rests on: at least one pair where neither is
    # better on both axes, so the frontier has something to report.
    assert len(fast_loop.pareto_frontier) >= 2
    assert len(fast_loop.pareto_frontier) < len(scored) or len(scored) == 2
    assert fast_loop.selection_rule == "quality_first"


@pytest.mark.asyncio
async def test_the_committed_candidate_is_on_the_frontier(tmp_path: Path) -> None:
    fast_loop = _fast_loop(await _run(tmp_path, design_search=True))
    assert fast_loop.selected_candidate_id is not None
    assert fast_loop.selected_candidate_id in fast_loop.pareto_frontier


@pytest.mark.asyncio
async def test_candidates_carry_a_measured_cost(tmp_path: Path) -> None:
    """Without this the cost axis is unavailable and the frontier is quality-only."""
    fast_loop = _fast_loop(await _run(tmp_path, design_search=True))
    executed = [c for c in fast_loop.candidates if c.cost.backend_calls]
    assert all(c.cost.estimated_cost_usd > 0.0 for c in executed)
    assert len({round(c.cost.estimated_cost_usd, 6) for c in executed}) > 1


@pytest.mark.asyncio
async def test_the_scalar_path_records_no_frontier(tmp_path: Path) -> None:
    """The control condition: same run, old selector, nothing to compare."""
    fast_loop = _fast_loop(await _run(tmp_path, design_search=False))
    assert fast_loop.pareto_frontier == []
    assert fast_loop.selection_rule == "scalar"
