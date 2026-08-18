"""One offline milestone, end to end, with the yardstick taken away mid-run.

The unit tests cover the pieces: the harness deletes the workspace copy, the
graph puts a custody step between the two agents, the prompts name the directory
to one agent and not the other. None of them answers the question the design
turns on — whether the implementer, running afterwards in a real workspace under
the real runtime, can still reach the suite. That needs the whole path: shared
workspace, harness subprocess, artifact wiring, rendered prompts.

Everything here is local. The two agents are scripted, the repository is four
files, and the harness is the one CodeProjectEval actually runs.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

from orchestra.backends.base import (
    AgentRequest,
    AgentResult,
    AgentRunStatus,
    BackendExecutionContext,
    BackendHealth,
)
from orchestra.backends.capabilities import BackendCapabilities, SessionPolicy
from orchestra.backends.registry import AgentBackendRegistry
from orchestra.codeprojecteval import (
    build_agent_workspace,
    check_command,
    load_task,
    materialize_check_harness,
)
from orchestra.codeprojecteval.harness import (
    build_deterministic_contracts,
    contracts_path_for,
    spec_tests_path_for,
)
from orchestra.executors.agent import AgentNodeExecutor
from orchestra.executors.harness import HarnessNodeExecutor
from orchestra.executors.registry import NodeExecutorRegistry
from orchestra.harness.repository_test import TRUSTED_MARKER
from orchestra.ir.artifacts import ArtifactBundle, create_artifact
from orchestra.ir.compiler import GraphCompiler
from orchestra.ir.contracts import load_contracts
from orchestra.ir.graph import OrchestraGraph
from orchestra.llm.usage import LLMUsage
from orchestra.realbench.milestone_planner import parse_plan_payload
from orchestra.realbench.subgraph_builder import (
    CODEPROJECTEVAL_PROMPT_PROFILE,
    materialize_milestone_subgraph,
    prepare_generated_root,
)
from orchestra.runtime.backend import RunContext
from orchestra.runtime.checkpoint import CheckpointStore
from orchestra.runtime.limits import RuntimeLimits, RuntimeSemaphores
from orchestra.runtime.native_async import NativeAsyncRuntime
from orchestra.sandbox.mock import MockSandbox
from orchestra.schemas.artifacts import ProblemArtifact, RepositoryChangeArtifact
from orchestra.storage.artifacts import FileArtifactStore
from orchestra.storage.events import AppendOnlyEventWriter

REFERENCE = (
    "class Widget:\n    value = 1\n\n    @staticmethod\n    def add(a, b):\n        return a + b\n"
)

#: The author's suite. `test_add_sums` is the string every later agent is checked
#: against: if it appears anywhere the implementer can reach, custody leaked.
AUTHORED_SUITE = """from demo_pkg import Widget


def test_value_is_one():
    assert Widget.value == 1


def test_add_sums():
    assert Widget.add(1, 2) == 3
"""


def _dataset(tmp_path: Path) -> Path:
    root = tmp_path / "dataset" / "demo"
    (root / "docs").mkdir(parents=True)
    (root / "demo_pkg").mkdir()
    (root / "check_tests").mkdir()
    (root / "unit_tests").mkdir()
    (root / "config.json").write_text(
        json.dumps(
            {
                "PRD": "docs/PRD.md",
                "UML": ["docs/UML.md"],
                "dependencies": "requirements.txt",
                "architecture_design": "docs/architecture_design.md",
                "language": "python",
                "source_code": "demo_pkg",
                "unit_tests": "unit_tests",
                "check_tests": "check_tests",
                "required_files": ["requirements.txt"],
            }
        ),
        encoding="utf-8",
    )
    (root / "docs" / "PRD.md").write_text("a widget adds numbers", encoding="utf-8")
    (root / "docs" / "UML.md").write_text("classDiagram", encoding="utf-8")
    (root / "docs" / "architecture_design.md").write_text("layers", encoding="utf-8")
    (root / "docs" / "directory_tree.txt").write_text(
        "├── demo_pkg\n│   ├── __init__.py\n│   └── core.py\n", encoding="utf-8"
    )
    (root / "requirements.txt").write_text("pytest\n", encoding="utf-8")
    (root / "demo_pkg" / "__init__.py").write_text(
        "from demo_pkg.core import Widget\n", encoding="utf-8"
    )
    (root / "demo_pkg" / "core.py").write_text(REFERENCE, encoding="utf-8")
    (root / "check_tests" / "test_widget.py").write_text(
        "from demo_pkg import Widget\n\n\ndef test_value():\n    assert Widget.value == 1\n",
        encoding="utf-8",
    )
    (root / "unit_tests" / "test_hidden.py").write_text(
        "def test_secret():\n    assert True\n", encoding="utf-8"
    )
    return root.parent


class ScriptedAgents:
    """The author writes the suite; everyone after it writes the implementation.

    It also keeps every request it was given, which is how the test asks what
    the implementer could actually see.
    """

    def __init__(self) -> None:
        self.requests: list[tuple[AgentRequest, list[str]]] = []

    @property
    def backend_id(self) -> str:
        return "codex_sdk"

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

    async def run(self, request: AgentRequest, context: BackendExecutionContext) -> AgentResult:
        workspace = Path(context.workspace_ref or "")
        listing = sorted(p.name for p in workspace.iterdir())
        self.requests.append((request, listing))

        if "test_author" in request.node_id:
            suite = workspace / "spec_tests"
            suite.mkdir(exist_ok=True)
            (suite / "test_spec.py").write_text(AUTHORED_SUITE, encoding="utf-8")
            changed = ["spec_tests/test_spec.py"]
            patch = "diff --git a/spec_tests/test_spec.py\n+" + AUTHORED_SUITE
        else:
            (workspace / "demo_pkg").mkdir(exist_ok=True)
            (workspace / "demo_pkg" / "__init__.py").write_text(
                "from demo_pkg.core import Widget\n", encoding="utf-8"
            )
            (workspace / "demo_pkg" / "core.py").write_text(REFERENCE, encoding="utf-8")
            changed = ["demo_pkg/core.py"]
            patch = "diff --git a/demo_pkg/core.py\n+" + REFERENCE

        artifact = create_artifact(
            RepositoryChangeArtifact(
                workspace_ref=str(workspace),
                thread_id=f"scripted-{len(self.requests)}",
                changed_files=changed,
                patch=patch,
                final_response="done",
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
            usage=LLMUsage(prompt_tokens=10, completion_tokens=1),
            backend_metadata={"workspace_ref": str(workspace)},
        )

    def seen_by(self, fragment: str) -> tuple[AgentRequest, list[str]]:
        return next((req, listing) for req, listing in self.requests if fragment in req.node_id)


async def _run(tmp_path: Path) -> tuple[ScriptedAgents, Path, dict]:
    task = load_task("demo", dataset_root=_dataset(tmp_path))
    workspace = build_agent_workspace(task, tmp_path / "ws")
    (workspace / TRUSTED_MARKER).write_text("", encoding="utf-8")

    harness_dir = tmp_path / "harness"
    materialize_check_harness(task, harness_dir=harness_dir, env_python=Path(sys.executable))
    contracts_path = contracts_path_for(harness_dir, "m1")
    contracts_path.write_text(
        json.dumps(build_deterministic_contracts(task, role="implementation", milestone_id="m1")),
        encoding="utf-8",
    )
    frozen = spec_tests_path_for(harness_dir, "m1")
    command = check_command(
        harness_dir=harness_dir,
        level="implementation",
        env_python=Path(sys.executable),
        contracts_path=contracts_path,
        spec_tests_path=frozen,
    )

    plan = parse_plan_payload(
        {
            "milestones": [
                {
                    "milestone_id": "m1",
                    "objective": "implement the widget",
                    "risk_rationale": "prose does not say how it behaves",
                    "template_id": "test_first",
                    "agents": [
                        {"slot": "test_author", "role": "test_author", "mandate": "a"},
                        {"slot": "builder", "role": "implementer", "mandate": "b"},
                    ],
                }
            ]
        },
        max_agents=4,
    )
    generated = prepare_generated_root(
        tmp_path / "generated", base_contracts_dir="configs/contracts"
    )
    graph_path, _roster = materialize_milestone_subgraph(
        generated_root=generated,
        milestone=plan.milestones[0],
        agent_backend="codex_sdk",
        harness_command=command,
        profile=CODEPROJECTEVAL_PROMPT_PROFILE,
    )
    payload = yaml.safe_load(Path(graph_path).read_text(encoding="utf-8"))
    contracts_dir = Path(payload["metadata"]["agent_roster"][0]["contract_path"]).parent
    compiled = GraphCompiler(
        contracts=load_contracts(str(contracts_dir)),
        harness_ids={"repository_test_harness"},
        transform_ids={"freeze_repository_change"},
        selector_ids=set(),
        backend_ids={"codex_sdk"},
    ).compile(OrchestraGraph(**payload))

    backend = ScriptedAgents()
    registry = AgentBackendRegistry()
    registry.register(backend)
    runtime = NativeAsyncRuntime(
        executors=NodeExecutorRegistry(
            agent_executor=AgentNodeExecutor(load_contracts(str(contracts_dir)), registry),
            harness_executor=HarnessNodeExecutor(MockSandbox()),
        ),
        artifact_store=FileArtifactStore(tmp_path / "run"),
        checkpoint_store=CheckpointStore(tmp_path / "run"),
        event_writer=AppendOnlyEventWriter(tmp_path / "run"),
    )
    limits = RuntimeLimits(
        max_parallel_benchmark_tasks=1,
        max_parallel_nodes_per_task=1,
        max_parallel_llm_calls=1,
        max_parallel_sandboxes=1,
    )
    problem = create_artifact(
        ProblemArtifact(
            question_id="demo",
            title="widget",
            statement="build the widget",
            difficulty="easy",
            platform="codeprojecteval",
        ),
        producer_node_id="__input__",
        task_id="custody",
    )
    result = await runtime.execute(
        graph=compiled,
        initial_artifacts=ArtifactBundle(slots={"problem": problem}),
        context=RunContext(
            run_id="custody-run",
            task_id="custody",
            run_dir=tmp_path / "run",
            limits=limits,
            semaphores=RuntimeSemaphores(limits),
            contract_hash="c",
            subtask_id="m1",
            workspace_ref=str(workspace),
        ),
    )
    report: dict = {}
    for artifact_id in result.state.node_outputs.get("repository_tests", {}).values():
        art = await runtime.artifact_store.get(artifact_id)
        report = art.payload
    return backend, workspace, report


@pytest.mark.asyncio
async def test_the_implementer_cannot_reach_the_suite_it_is_scored_on(
    tmp_path: Path,
) -> None:
    backend, workspace, _report = await _run(tmp_path)
    _author_request, author_saw = backend.seen_by("test_author")
    builder_request, builder_saw = backend.seen_by("builder")

    assert "spec_tests" not in author_saw, "the author starts from a clean repository"
    assert "spec_tests" not in builder_saw, "custody left the suite in the workspace"
    assert not (workspace / "spec_tests").exists()
    # Nor through the prompt: the author's patch quotes the suite in full, which
    # is why the change edge between the two agents is cut rather than kept.
    assert "test_add_sums" not in builder_request.rendered_context
    assert "test_add_sums" not in builder_request.instruction
    # Nor by name. The builder waits on the custody report, whose own output says
    # what it moved and where it put it — an absolute path to the yardstick,
    # handed to an agent that can read the filesystem.
    assert "spec_tests" not in builder_request.rendered_context


@pytest.mark.asyncio
async def test_the_milestone_is_still_scored_on_the_hidden_suite(
    tmp_path: Path,
) -> None:
    """Hiding it has to cost nothing, or the axis is gone rather than fixed."""
    _backend, _workspace, report = await _run(tmp_path)
    stages = {stage["stage"]: stage for stage in report.get("stages") or []}

    assert "spec_tests" in stages, f"no behavioural stage in {report.get('stages')}"
    assert stages["spec_tests"]["total_units"] == 2
    assert stages["spec_tests"]["passed_units"] == 2
    assert report["passed"] is True
