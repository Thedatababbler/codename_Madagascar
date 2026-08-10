"""Shared rig for driving the fast loop end to end against a scripted harness.

Both the design-search tests and the quality-trigger tests need the same thing: a
real scheduler run over a small repository, a backend whose spend differs per call
so the cost axis is not flat, and a harness whose verdict and score are decided by
the test rather than by the code. The only difference between them is what the
harness says on the *first* invocation -- fail for a repair search, pass with a poor
score for a quality search -- so that is the parameter.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

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
from orchestra.control.fast_loop.quality_trigger import QualityTrigger
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

#: Verdict and score per harness invocation, in order: the first attempt, then one
#: per candidate, then the commit's re-verification. The commit re-runs the harness
#: on the canonical workspace, so the tail must pass or the winner is rolled back
#: and there is nothing left to assert about which design was chosen.
HARNESS_SCRIPT = """
import json, sys
from pathlib import Path

counter = Path(r"{counter}")
n = int(counter.read_text()) + 1 if counter.exists() else 1
counter.write_text(str(n))
scores = {scores}
passes = {passes}
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


class CountingBackend:
    """Spends more on every call, so candidates differ on the cost axis.

    Candidates that all cost the same cannot produce a trade-off, and a frontier
    with nothing to trade is exactly the degenerate case these tests exist to rule
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


def harness_command(
    tmp_path: Path, *, scores: Sequence[float], passes: Sequence[bool]
) -> list[str]:
    script = tmp_path / "staged_harness.py"
    script.write_text(
        HARNESS_SCRIPT.format(
            counter=tmp_path / "harness_calls.txt",
            scores=list(scores),
            passes=list(passes),
        ),
        encoding="utf-8",
    )
    return ["python", str(script)]


def plan(tmp_path: Path, command: list[str]) -> TaskPlan:
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


def context(tmp_path: Path) -> RunContext:
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


def bundle() -> ArtifactBundle:
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


def runtime(tmp_path: Path, registry: AgentBackendRegistry) -> NativeAsyncRuntime:
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


async def run_scheduler(
    tmp_path: Path,
    *,
    design_search: bool,
    scores: Sequence[float],
    passes: Sequence[bool],
    quality_trigger: QualityTrigger | None = None,
) -> TaskExecutionState:
    registry = AgentBackendRegistry()
    registry.register(CountingBackend())
    rt = runtime(tmp_path, registry)
    task_plan = plan(tmp_path, harness_command(tmp_path, scores=scores, passes=passes))
    scheduler = ReadySubtaskScheduler(
        runtime=rt,
        artifact_store=rt.artifact_store,
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
        quality_trigger=quality_trigger,
    )
    state = TaskExecutionState.from_plan(
        task_plan, artifact_store_ref=str(tmp_path / "artifacts")
    )
    return await scheduler.run_task(
        task_plan,
        state,
        initial_artifacts=bundle(),
        context=context(tmp_path),
        source_repo=str(FIXTURE),
    )
