"""Opt-in production M6 path: TaskPlan → ReadySubtaskScheduler → Pareto Slow Loop.

Does not wrap SingleSubtaskCompatibilityRunner or private smoke helpers.
Pareto-disabled runs keep the existing single-subtask Stage-1 behaviour via
``run_task_plan``; this entry point is for multi-subtask / Pareto production.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from orchestra.backends.factory import build_default_backend_registry
from orchestra.backends.health import healthcheck_used_backends
from orchestra.cli.run import _mock_client
from orchestra.cli.validate_graph import build_compiler
from orchestra.config import load_experiment_config, load_experiment_raw
from orchestra.control.fast_loop.schemas import FastLoopBudget
from orchestra.control.pareto.runtime_factory import (
    build_slow_loop_controller,
    control_plane_manifest_fields,
    resolve_from_mapping,
)
from orchestra.control.ready_scheduler import ReadySubtaskScheduler
from orchestra.control.task_state import TaskExecutionState
from orchestra.decomposition.decomposer import TaskDecomposer
from orchestra.decomposition.schemas import TaskPlan
from orchestra.executors.agent import AgentNodeExecutor
from orchestra.executors.harness import HarnessNodeExecutor
from orchestra.executors.registry import NodeExecutorRegistry
from orchestra.experiments.metadata import git_commit_hash
from orchestra.ir.artifacts import ArtifactBundle, create_artifact
from orchestra.ir.contracts import load_contracts
from orchestra.ir.graph import load_graph
from orchestra.llm.openai_compatible_async import OpenAICompatibleAsyncClient
from orchestra.runtime.backend import RunContext
from orchestra.runtime.checkpoint import CheckpointStore
from orchestra.runtime.limits import RuntimeSemaphores
from orchestra.runtime.native_async import NativeAsyncRuntime
from orchestra.runtime.task_checkpoint import TaskCheckpointStore
from orchestra.sandbox.docker import DockerSandbox, DockerUnavailableError
from orchestra.sandbox.lcb_official import (
    OfficialLCBSandbox,
    OfficialLCBSandboxUnavailable,
)
from orchestra.sandbox.mock import MockSandbox
from orchestra.schemas.artifacts import ProblemArtifact
from orchestra.settings import load_env_file
from orchestra.storage.artifacts import FileArtifactStore
from orchestra.storage.events import AppendOnlyEventWriter
from orchestra.telemetry.events import TelemetryEvent


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _load_plan(path: Path) -> TaskPlan:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return TaskPlan.model_validate(raw)


def _graph_catalog_hash(plan: TaskPlan) -> str:
    hashes: list[str] = []
    for sub in plan.subtasks:
        gpath = sub.local_graph_template
        if not gpath:
            continue
        graph = load_graph(gpath)
        hashes.append(f"{sub.subtask_id}:{graph.content_hash}")
    return hashlib.sha256("|".join(sorted(hashes)).encode()).hexdigest()


async def _run(args: argparse.Namespace) -> int:
    load_env_file()
    started_at = datetime.now(UTC)
    repo_root = _repo_root()
    raw = load_experiment_raw(args.config)
    config = load_experiment_config(args.config)
    resolved = resolve_from_mapping(raw, repo_root=repo_root)

    if resolved.pareto_config.enabled and not resolved.slow_loop_config.enabled:
        raise SystemExit("pareto.enabled=true requires slow_loop.enabled=true")

    plan_path = (
        args.plan
        or config.experiment.plan_config
        or (raw.get("experiment") or {}).get("plan_config")
    )
    if not plan_path:
        raise SystemExit(
            "run_m6_orchestra requires experiment.plan_config (multi-subtask TaskPlan)"
        )

    if args.output_root:
        config.experiment.output_root = args.output_root

    contracts_dir = config.experiment.contracts_dir
    contracts = load_contracts(contracts_dir)
    contract_hash = hashlib.sha256(
        "".join(contracts[key].model_dump_json() for key in sorted(contracts)).encode()
    ).hexdigest()

    candidate = _load_plan(Path(plan_path))
    decomposer = TaskDecomposer(
        enabled=True,
        default_graph_template=config.experiment.graph_config,
        keystone_harness_id=candidate.subtasks[0].keystone_harness_id or "none",
        require_graph_files=True,
    )
    plan = decomposer.decompose(
        task_id=candidate.task_id,
        objective=candidate.subtasks[0].objective,
        candidate_plan=candidate,
        metadata={"plan_config": str(plan_path), "runner": "run_m6_orchestra"},
    )

    run_id = args.run_id or (
        f"m6orch-{config.experiment.name}-{started_at.strftime('%Y%m%dT%H%M%SZ')}"
    )
    run_dir = Path(config.experiment.output_root) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    llm = _mock_client() if args.mock_llm else OpenAICompatibleAsyncClient()
    if args.mock_llm or config.sandbox.backend == "mock":
        sandbox = MockSandbox()
    elif config.sandbox.backend == "lcb_official":
        try:
            sandbox = OfficialLCBSandbox(
                repository_path=config.benchmark.repository_path,
                limits=config.sandbox.limits,
                num_process_evaluate=config.sandbox.num_process_evaluate,
                worker_grace_seconds=config.sandbox.worker_grace_seconds,
                max_worker_wall_seconds=config.sandbox.max_worker_wall_seconds,
            )
        except OfficialLCBSandboxUnavailable as exc:
            raise RuntimeError(
                "OfficialLCBSandbox is unavailable; refusing subprocess fallback."
            ) from exc
    elif config.sandbox.backend == "docker":
        try:
            sandbox = DockerSandbox(
                image=config.sandbox.image,
                timeout_seconds=config.sandbox.per_test_timeout_seconds,
                memory_mb=config.sandbox.limits.memory_mb,
            )
        except DockerUnavailableError as exc:
            raise RuntimeError("Docker backend is unavailable.") from exc
    else:
        raise RuntimeError("unsupported sandbox backend for M6 orchestra runner")

    backend_registry = build_default_backend_registry(
        llm, include_smolagents=True, include_codex=True
    )
    semaphores = RuntimeSemaphores(config.runtime)
    artifact_store = FileArtifactStore(run_dir)
    checkpoint_store = CheckpointStore(run_dir)
    task_checkpoint_store = TaskCheckpointStore(run_dir)
    event_writer = AppendOnlyEventWriter(run_dir)
    runtime = NativeAsyncRuntime(
        executors=NodeExecutorRegistry(
            agent_executor=AgentNodeExecutor(contracts, backend_registry),
            harness_executor=HarnessNodeExecutor(
                sandbox, timeout_seconds=config.sandbox.per_test_timeout_seconds
            ),
        ),
        artifact_store=artifact_store,
        checkpoint_store=checkpoint_store,
        event_writer=event_writer,
    )

    first_graph = load_graph(plan.subtasks[0].local_graph_template)
    compiled = build_compiler(contracts_dir).compile(first_graph)
    if not args.skip_healthcheck:
        await healthcheck_used_backends(
            registry=backend_registry,
            graph=compiled,
            event_writer=event_writer,
            run_id=run_id,
            graph_id=compiled.graph.graph_id,
        )

    # Fail closed when Pareto is requested but components cannot initialize.
    slow_loop = build_slow_loop_controller(
        resolved,
        run_dir=run_dir,
        contracts_dir=contracts_dir,
        repo_root=repo_root,
        checkpoint_store=task_checkpoint_store,
        fail_closed=True,
    )

    source_repo = (
        args.source_repo
        or config.experiment.source_repo
        or (raw.get("experiment") or {}).get("source_repo")
    )
    scheduler = ReadySubtaskScheduler(
        runtime=runtime,
        artifact_store=artifact_store,
        task_checkpoint_store=task_checkpoint_store,
        contracts_dir=contracts_dir,
        source_repo=str(Path(source_repo).resolve()) if source_repo else None,
        budget=FastLoopBudget(max_candidates=2, max_total_backend_calls=6),
        slow_loop=slow_loop,
        slow_loop_config=resolved.slow_loop_config,
        max_concurrent_subtasks=1,
    )

    problem = ProblemArtifact(
        question_id=plan.task_id,
        title=plan.task_id,
        statement=plan.subtasks[0].objective,
        difficulty="fixture",
        platform="orchestra",
    )
    initial = create_artifact(
        problem, producer_node_id="__input__", task_id=plan.task_id
    )
    context = RunContext(
        run_id=run_id,
        task_id=plan.task_id,
        run_dir=run_dir,
        limits=config.runtime,
        semaphores=semaphores,
        contract_hash=contract_hash,
        allow_config_drift=args.allow_config_drift,
    )
    state = TaskExecutionState.from_plan(
        plan, artifact_store_ref=str(run_dir / "artifacts")
    )
    # Resume from checkpoint when present.
    loaded = await task_checkpoint_store.load(
        plan.task_id,
        plan_version=plan.plan_version,
        plan_content_hash=plan.content_hash(),
        allow_config_drift=args.allow_config_drift,
    )
    if loaded is not None:
        state = loaded

    manifest: dict[str, Any] = {
        "runner": "run_m6_orchestra",
        "started_at": started_at.isoformat(),
        "git_commit": git_commit_hash(repo_root),
        "config_path": str(args.config),
        "plan_config": str(plan_path),
        "experiment": config.model_dump(mode="json"),
        "manifest_hash": hashlib.sha256(
            Path(config.benchmark.manifest).read_bytes()
            if Path(config.benchmark.manifest).exists()
            else b""
        ).hexdigest(),
        "graph_catalog_hash": _graph_catalog_hash(plan),
        "contract_hash": contract_hash,
        **control_plane_manifest_fields(resolved),
    }
    (run_dir / "run_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    await event_writer.append(
        TelemetryEvent(
            run_id=run_id,
            task_id=plan.task_id,
            graph_id=compiled.graph.graph_id,
            event_type="m6_orchestra_started",
            payload={"pareto_enabled": resolved.pareto_config.enabled},
        )
    )

    state = await scheduler.run_task(
        plan,
        state,
        initial_artifacts=ArtifactBundle(slots={"problem": initial}),
        context=context,
        source_repo=str(Path(source_repo).resolve()) if source_repo else None,
    )

    summary = {
        "run_dir": str(run_dir),
        "task_id": plan.task_id,
        "pareto_enabled": resolved.pareto_config.enabled,
        "active_plan_revision_id": state.active_plan_revision_id,
        "committed": [
            sid
            for sid, sub in state.subtasks.items()
            if sub.status.value == "committed"
        ],
        "decisions": (
            len(state.pareto_state.decision_history)
            if state.pareto_state is not None
            else 0
        ),
        "pending_decision": bool(
            state.pareto_state and state.pareto_state.pending_decision
        ),
        "control_plane_hash": resolved.control_plane_hash,
    }
    (run_dir / "m6_orchestra_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--plan", default=None)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--output-root", default=None)
    parser.add_argument("--source-repo", default=None)
    parser.add_argument("--mock-llm", action="store_true")
    parser.add_argument("--allow-config-drift", action="store_true")
    parser.add_argument("--skip-healthcheck", action="store_true")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
