"""E2E: fixture smolagents model edits workspace → git artifact → harness → commit."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from orchestra.backends.registry import AgentBackendRegistry
from orchestra.backends.smolagents_code import SmolagentsCodeBackend
from orchestra.control.fast_loop.schemas import FastLoopBudget
from orchestra.control.ready_scheduler import ReadySubtaskScheduler
from orchestra.control.slow_loop.controller import SlowLoopController
from orchestra.control.slow_loop.schemas import SlowLoopBudget, SlowLoopConfig
from orchestra.control.task_state import SubtaskStatus, TaskExecutionState
from orchestra.decomposition.schemas import TaskPlan
from orchestra.executors.agent import AgentNodeExecutor
from orchestra.executors.harness import HarnessNodeExecutor
from orchestra.executors.registry import NodeExecutorRegistry
from orchestra.ir.artifacts import ArtifactBundle, create_artifact
from orchestra.ir.contracts import load_contracts
from orchestra.ir.graph import load_graph
from orchestra.realbench.public_harness import (
    materialize_public_harness,
    public_check_command,
)
from orchestra.runtime.backend import RunContext
from orchestra.runtime.checkpoint import CheckpointStore
from orchestra.runtime.limits import RuntimeLimits, RuntimeSemaphores
from orchestra.runtime.native_async import NativeAsyncRuntime
from orchestra.runtime.task_checkpoint import TaskCheckpointStore
from orchestra.sandbox.mock import MockSandbox
from orchestra.schemas.artifacts import ProblemArtifact, RepositoryChangeArtifact
from orchestra.storage.artifacts import FileArtifactStore
from orchestra.storage.events import AppendOnlyEventWriter

CONTRACTS = "configs/contracts"


def _fixture_edit_response(path: str = "demo_pkg/core.py", content: str = "x = 1\n") -> str:
    code = (
        f"write_workspace_file({path!r}, {content!r})\n"
        "final_answer('edited via repository tool')"
    )
    return json.dumps({"thought": "edit real workspace file", "code": code})


def _harness_dir(tmp_path: Path) -> Path:
    return tmp_path / "adamas_harness"


def _git_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "source_repo"
    public = ws / "public_design"
    public.mkdir(parents=True)
    (public / "tree.txt").write_text(
        "proj_clean/\n└── demo_pkg/\n    ├── __init__.py\n    └── core.py\n",
        encoding="utf-8",
    )
    (public / "package.json").write_text(
        '{"packageDiagram":{"packages":[{"name":"core","type":"package","exports":[]}]}}',
        encoding="utf-8",
    )
    (ws / "demo_pkg").mkdir()
    (ws / "demo_pkg" / "__init__.py").write_text("", encoding="utf-8")
    (ws / "demo_pkg" / "core.py").write_text("# scaffold\n", encoding="utf-8")
    (ws / ".adamas_trusted_harness").write_text("trusted_fixture\n", encoding="utf-8")
    (ws / "TASK.md").write_text("# task\n", encoding="utf-8")
    materialize_public_harness(ws, harness_dir=_harness_dir(tmp_path))
    subprocess.run(["git", "init"], cwd=ws, check=True, capture_output=True)
    subprocess.run(["git", "-C", str(ws), "config", "user.email", "t@local"], check=True)
    subprocess.run(["git", "-C", str(ws), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(ws), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(ws), "commit", "-m", "pristine"],
        check=True,
        capture_output=True,
    )
    return ws


def _materialize_fixture_graph(tmp_path: Path, *, empty_edit: bool = False) -> Path:
    base = load_graph("configs/graphs/smolagents_realbench_public_discovery.yaml")
    raw = yaml.safe_load(
        Path("configs/graphs/smolagents_realbench_public_discovery.yaml").read_text(
            encoding="utf-8"
        )
    )
    response = (
        json.dumps(
            {
                "thought": "no edit",
                "code": "final_answer('I claim a patch but did nothing')",
            }
        )
        if empty_edit
        else _fixture_edit_response()
    )
    harness_command = public_check_command(
        harness_dir=_harness_dir(tmp_path), level="discovery"
    )
    for node in raw["nodes"]:
        if node.get("node_kind") == "agent":
            node["model"] = {"provider": "fixture", "name": "scripted-repo"}
            node["backend"]["fixture_responses"] = [response]
            node["backend"]["max_steps"] = 6
            node["timeout_seconds"] = 120
        if node.get("node_kind") == "harness":
            node["command"] = harness_command
    out = tmp_path / "smolagents_fixture_discovery.yaml"
    out.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    # Ensure loadable
    g = load_graph(out)
    assert g.graph_id == base.graph_id
    return out


def _plan(graph_path: Path, *, milestone_brief: str | None = None) -> TaskPlan:
    metadata: dict = {"role": "discovery", "public_harness_level": "discovery"}
    if milestone_brief is not None:
        metadata["milestone_brief"] = milestone_brief
    payload = {
        "task_id": "smol_rb_e2e",
        "plan_version": 1,
        "decomposition_rationale": "smolagents repository editing e2e",
        "decomposition_status": "ok",
        "subtasks": [
            {
                "subtask_id": "map_public_api",
                "title": "Discovery milestone",
                "objective": "Edit workspace so discovery public check passes",
                "dependencies": [],
                "keystone_harness_id": "repository_test_harness",
                "local_graph_template": str(graph_path),
                "budget": {
                    "max_llm_calls": 4,
                    "max_steps": 6,
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
                "metadata": metadata,
            }
        ],
        "final_aggregation": {
            "strategy": "identity",
            "terminal_subtask_id": "map_public_api",
        },
        "communication_plan": {"version": 1},
    }
    return TaskPlan.model_validate(payload)


def _runtime(run_dir: Path, registry: AgentBackendRegistry) -> NativeAsyncRuntime:
    contracts = load_contracts(CONTRACTS)
    return NativeAsyncRuntime(
        executors=NodeExecutorRegistry(
            agent_executor=AgentNodeExecutor(contracts, registry),
            harness_executor=HarnessNodeExecutor(MockSandbox(), timeout_seconds=60),
        ),
        artifact_store=FileArtifactStore(run_dir),
        checkpoint_store=CheckpointStore(run_dir),
        event_writer=AppendOnlyEventWriter(run_dir),
    )


@pytest.mark.asyncio
async def test_smolagents_repo_edit_to_canonical_commit(tmp_path: Path) -> None:
    source = _git_workspace(tmp_path)
    graph_path = _materialize_fixture_graph(tmp_path)
    plan = _plan(
        graph_path,
        milestone_brief="# Milestone\nEdit demo_pkg/core.py with repository tools.\n",
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    registry = AgentBackendRegistry()
    registry.register(SmolagentsCodeBackend())  # real backend; fixture model only
    runtime = _runtime(run_dir, registry)
    limits = RuntimeLimits(
        max_parallel_benchmark_tasks=1,
        max_parallel_nodes_per_task=2,
        max_parallel_llm_calls=1,
        max_parallel_sandboxes=1,
        max_concurrent_subtasks=1,
    )
    slow = SlowLoopController(
        config=SlowLoopConfig(
            enabled=False,
            budget=SlowLoopBudget(max_updates_per_task=0, max_candidates_per_update=0),
            allowed_backend_assignments={"coding": ["smolagents_code"]},
            backend_model_pools={"smolagents_code": ["scripted-repo"]},
        ),
        checkpoint_store=TaskCheckpointStore(run_dir),
    )
    scheduler = ReadySubtaskScheduler(
        runtime=runtime,
        artifact_store=FileArtifactStore(run_dir),
        task_checkpoint_store=TaskCheckpointStore(run_dir),
        contracts_dir=CONTRACTS,
        source_repo=str(source.resolve()),
        budget=FastLoopBudget(max_candidates=0, max_total_backend_calls=0),
        slow_loop=slow,
        slow_loop_config=slow.config,
        max_concurrent_subtasks=1,
        allow_concurrent_subtasks=False,
    )
    problem = ProblemArtifact(
        question_id="smol_rb_e2e",
        title="smol",
        statement="Edit the workspace with repository tools.",
        difficulty="level2",
        platform="realbench",
        starter_code="",
        public_examples=[],
    )
    initial = create_artifact(problem, producer_node_id="__input__", task_id=plan.task_id)
    state = TaskExecutionState.from_plan(
        plan, artifact_store_ref=str(run_dir / "artifacts")
    )
    context = RunContext(
        run_id="smol-rb-e2e",
        task_id=plan.task_id,
        run_dir=run_dir,
        limits=limits,
        semaphores=RuntimeSemaphores(limits),
        contract_hash="test",
    )
    state = await scheduler.run_task(
        plan,
        state,
        initial_artifacts=ArtifactBundle(slots={"problem": initial}),
        context=context,
        source_repo=str(source.resolve()),
    )
    sub = state.subtasks["map_public_api"]
    assert sub.status is SubtaskStatus.COMMITTED, (
        sub.status,
        sub.failure_reason,
        sub.failure_message,
        [a.model_dump(mode="json") for a in (sub.attempts or [])],
    )
    assert state.workspace_commit_records
    commit = state.workspace_commit_records[-1]
    assert commit.subtask_id == "map_public_api"
    assert commit.attempt_id == sub.attempts[-1].attempt_id
    # Filesystem evidence: agent edit present in committed canonical workspace.
    canonical = Path(state.canonical_workspace_ref or "")
    assert canonical.is_dir()
    assert "x = 1" in (canonical / "demo_pkg" / "core.py").read_text(encoding="utf-8")
    # Artifact / evaluation / usage join the same attempt.
    attempt = sub.attempts[-1]
    assert attempt.attempt_id == commit.attempt_id
    arts = list(commit.applied_artifact_ids or [])
    if sub.final_output_artifact_id:
        arts.append(sub.final_output_artifact_id)
    arts.extend(ref.artifact_id for ref in (sub.committed_artifacts or []))
    assert arts
    usage = [
        u
        for u in (state.backend_usage_records or [])
        if int(u.attempt_id) == int(attempt.attempt_id)
        and u.backend_id == "smolagents_code"
    ]
    assert usage
    store = FileArtifactStore(run_dir)
    found_change = False
    for aid in arts:
        env = await store.get(aid)
        if env.artifact_type == "RepositoryChangeArtifact":
            change = RepositoryChangeArtifact.model_validate(env.payload)
            assert "demo_pkg/core.py" in change.changed_files
            assert "x = 1" in change.patch or "demo_pkg/core.py" in change.patch
            assert change.workspace_ref
            found_change = True
    assert found_change
    # Public harness gated the freeze (commit would not exist otherwise).
    assert commit.status.value == "committed"
    assert attempt.usage_ids


@pytest.mark.asyncio
async def test_empty_workspace_change_does_not_fake_success(tmp_path: Path) -> None:
    source = _git_workspace(tmp_path)
    graph_path = _materialize_fixture_graph(tmp_path, empty_edit=True)
    plan = _plan(graph_path, milestone_brief=None)
    run_dir = tmp_path / "run_empty"
    run_dir.mkdir()
    registry = AgentBackendRegistry()
    registry.register(SmolagentsCodeBackend())
    runtime = _runtime(run_dir, registry)
    limits = RuntimeLimits(
        max_parallel_benchmark_tasks=1,
        max_parallel_nodes_per_task=2,
        max_parallel_llm_calls=1,
        max_parallel_sandboxes=1,
        max_concurrent_subtasks=1,
    )
    slow_cfg = SlowLoopConfig(
        enabled=False,
        budget=SlowLoopBudget(max_updates_per_task=0, max_candidates_per_update=0),
        allowed_backend_assignments={"coding": ["smolagents_code"]},
        backend_model_pools={"smolagents_code": ["scripted-repo"]},
    )
    scheduler = ReadySubtaskScheduler(
        runtime=runtime,
        artifact_store=FileArtifactStore(run_dir),
        task_checkpoint_store=TaskCheckpointStore(run_dir),
        contracts_dir=CONTRACTS,
        source_repo=str(source.resolve()),
        budget=FastLoopBudget(max_candidates=0, max_total_backend_calls=0),
        slow_loop=SlowLoopController(
            config=slow_cfg, checkpoint_store=TaskCheckpointStore(run_dir)
        ),
        slow_loop_config=slow_cfg,
        max_concurrent_subtasks=1,
        allow_concurrent_subtasks=False,
    )
    problem = ProblemArtifact(
        question_id="smol_rb_empty",
        title="smol",
        statement="do not edit",
        difficulty="level2",
        platform="realbench",
        starter_code="",
        public_examples=[],
    )
    initial = create_artifact(
        problem, producer_node_id="__input__", task_id=plan.task_id
    )
    state = TaskExecutionState.from_plan(
        plan, artifact_store_ref=str(run_dir / "artifacts")
    )
    context = RunContext(
        run_id="smol-rb-empty",
        task_id=plan.task_id,
        run_dir=run_dir,
        limits=limits,
        semaphores=RuntimeSemaphores(limits),
        contract_hash="test",
    )
    state = await scheduler.run_task(
        plan,
        state,
        initial_artifacts=ArtifactBundle(slots={"problem": initial}),
        context=context,
        source_repo=str(source.resolve()),
    )
    sub = state.subtasks["map_public_api"]
    assert sub.status is not SubtaskStatus.COMMITTED
    assert all(
        c.subtask_id != "map_public_api" for c in (state.workspace_commit_records or [])
    )


@pytest.mark.asyncio
async def test_public_harness_failure_blocks_commit(tmp_path: Path) -> None:
    """Broken Python that fails compileall must not commit."""
    source = _git_workspace(tmp_path)
    graph_path = _materialize_fixture_graph(tmp_path)
    # Override fixture to write syntax-invalid Python.
    raw = yaml.safe_load(graph_path.read_text(encoding="utf-8"))
    bad = json.dumps(
        {
            "thought": "break compile",
            "code": (
                "write_workspace_file('demo_pkg/core.py', 'def broken(\\n')\n"
                "final_answer('broken')"
            ),
        }
    )
    for node in raw["nodes"]:
        if node.get("node_kind") == "agent":
            node["backend"]["fixture_responses"] = [bad]
    graph_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    plan = _plan(graph_path)
    run_dir = tmp_path / "run_fail_harness"
    run_dir.mkdir()
    registry = AgentBackendRegistry()
    registry.register(SmolagentsCodeBackend())
    runtime = _runtime(run_dir, registry)
    limits = RuntimeLimits(
        max_parallel_benchmark_tasks=1,
        max_parallel_nodes_per_task=2,
        max_parallel_llm_calls=1,
        max_parallel_sandboxes=1,
        max_concurrent_subtasks=1,
    )
    slow_cfg = SlowLoopConfig(
        enabled=False,
        budget=SlowLoopBudget(max_updates_per_task=0, max_candidates_per_update=0),
        allowed_backend_assignments={"coding": ["smolagents_code"]},
        backend_model_pools={"smolagents_code": ["scripted-repo"]},
    )
    scheduler = ReadySubtaskScheduler(
        runtime=runtime,
        artifact_store=FileArtifactStore(run_dir),
        task_checkpoint_store=TaskCheckpointStore(run_dir),
        contracts_dir=CONTRACTS,
        source_repo=str(source.resolve()),
        budget=FastLoopBudget(max_candidates=0, max_total_backend_calls=0),
        slow_loop=SlowLoopController(
            config=slow_cfg, checkpoint_store=TaskCheckpointStore(run_dir)
        ),
        slow_loop_config=slow_cfg,
        max_concurrent_subtasks=1,
        allow_concurrent_subtasks=False,
    )
    problem = ProblemArtifact(
        question_id="smol_rb_harness_fail",
        title="smol",
        statement="break",
        difficulty="level2",
        platform="realbench",
        starter_code="",
        public_examples=[],
    )
    initial = create_artifact(
        problem, producer_node_id="__input__", task_id=plan.task_id
    )
    state = TaskExecutionState.from_plan(
        plan, artifact_store_ref=str(run_dir / "artifacts")
    )
    context = RunContext(
        run_id="smol-rb-harness-fail",
        task_id=plan.task_id,
        run_dir=run_dir,
        limits=limits,
        semaphores=RuntimeSemaphores(limits),
        contract_hash="test",
    )
    state = await scheduler.run_task(
        plan,
        state,
        initial_artifacts=ArtifactBundle(slots={"problem": initial}),
        context=context,
        source_repo=str(source.resolve()),
    )
    sub = state.subtasks["map_public_api"]
    assert sub.status is not SubtaskStatus.COMMITTED
