"""Opt-in CLI: run LiveCodeBench tasks through TaskPlan single-subtask IR."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from orchestra.adapters.livecodebench.loader import LiveCodeBenchLoader
from orchestra.adapters.livecodebench.mapper import load_manifest
from orchestra.backends.factory import build_default_backend_registry
from orchestra.backends.health import healthcheck_used_backends
from orchestra.cli.run import _mock_client, _MockAwareAgentNodeExecutor
from orchestra.cli.validate_graph import build_compiler
from orchestra.config import load_experiment_config
from orchestra.control.single_subtask import (
    SingleSubtaskCompatibilityRunner,
    summarize_task_state,
)
from orchestra.decomposition.decomposer import TaskDecomposer
from orchestra.executors.agent import AgentNodeExecutor
from orchestra.executors.harness import HarnessNodeExecutor
from orchestra.executors.registry import NodeExecutorRegistry
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
from orchestra.settings import load_env_file
from orchestra.storage.artifacts import FileArtifactStore
from orchestra.storage.events import AppendOnlyEventWriter
from orchestra.telemetry.events import TelemetryEvent


def _keystone_for_graph(graph_path: str) -> str:
    graph = load_graph(graph_path)
    has_harness = any(node.node_kind.value == "harness" for node in graph.nodes)
    return "public_code_harness" if has_harness else "none"


async def _run(args: argparse.Namespace) -> int:
    load_env_file()
    started_at = datetime.now(UTC)
    config = load_experiment_config(args.config)
    # Pareto-enabled production must use ReadySubtaskScheduler via run_m6_orchestra.
    # Do not silently wrap SingleSubtaskCompatibilityRunner.
    if bool((config.pareto or {}).get("enabled", False)):
        raise SystemExit(
            "pareto.enabled=true is not supported by run_task_plan "
            "(SingleSubtaskCompatibilityRunner). Use: "
            "python -m orchestra.cli.run_m6_orchestra --config <cfg>"
        )
    if args.manifest:
        config.benchmark.manifest = args.manifest
    if args.output_root:
        config.experiment.output_root = args.output_root

    graph_path = args.graph or config.experiment.graph_config
    contracts = load_contracts(config.experiment.contracts_dir)
    contract_hash = hashlib.sha256(
        "".join(
            contracts[key].model_dump_json() for key in sorted(contracts)
        ).encode()
    ).hexdigest()

    manifest = load_manifest(config.benchmark.manifest)
    ids = [entry.question_id for entry in manifest.entries]
    if args.task_id:
        ids = [qid for qid in ids if qid == args.task_id]
    if args.limit is not None:
        ids = ids[: args.limit]

    decomposer = TaskDecomposer(
        enabled=False,
        default_graph_template=graph_path,
        keystone_harness_id=_keystone_for_graph(graph_path),
        require_graph_files=True,
    )

    if args.dry_run:
        sample = decomposer.decompose(
            task_id=ids[0] if ids else "dry_run",
            objective="dry-run",
        )
        print(
            json.dumps(
                {
                    "mode": "task_plan_single_subtask",
                    "task_ids": ids,
                    "graph": graph_path,
                    "decomposition_status": sample.decomposition_status.value,
                    "plan_version": sample.plan_version,
                    "started_at": started_at.isoformat(),
                },
                indent=2,
            )
        )
        return 0

    loader = LiveCodeBenchLoader(
        config.benchmark.data_dir, config.benchmark.release_version
    )
    tasks = loader.load(task_ids=set(ids))
    task_map = {task.question_id: task for task in tasks}

    run_id = args.run_id or (
        f"taskplan-{config.experiment.name}-{manifest.manifest_sha256[:8]}"
    )
    run_dir = Path(config.experiment.output_root) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    llm = _mock_client() if args.mock_llm else OpenAICompatibleAsyncClient()
    if args.mock_llm:
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
            raise RuntimeError(
                "Docker backend is unavailable; select lcb_official explicitly."
            ) from exc
    else:
        raise RuntimeError("sandbox.backend=mock is permitted only with --mock-llm")

    semaphores = RuntimeSemaphores(config.runtime)
    artifact_store = FileArtifactStore(run_dir)
    checkpoint_store = CheckpointStore(run_dir)
    task_checkpoint_store = TaskCheckpointStore(run_dir)
    event_writer = AppendOnlyEventWriter(run_dir)
    backend_registry = build_default_backend_registry(llm, include_smolagents=True)
    agent_executor_cls = (
        _MockAwareAgentNodeExecutor if args.mock_llm else AgentNodeExecutor
    )
    runtime = NativeAsyncRuntime(
        executors=NodeExecutorRegistry(
            agent_executor=agent_executor_cls(contracts, backend_registry),
            harness_executor=HarnessNodeExecutor(
                sandbox, timeout_seconds=config.sandbox.per_test_timeout_seconds
            ),
        ),
        artifact_store=artifact_store,
        checkpoint_store=checkpoint_store,
        event_writer=event_writer,
    )
    runner = SingleSubtaskCompatibilityRunner(
        runtime=runtime,
        artifact_store=artifact_store,
        task_checkpoint_store=task_checkpoint_store,
        contracts_dir=config.experiment.contracts_dir,
    )

    compiled = build_compiler(config.experiment.contracts_dir).compile(
        load_graph(graph_path)
    )
    await healthcheck_used_backends(
        registry=backend_registry,
        graph=compiled,
        event_writer=event_writer,
        run_id=run_id,
        graph_id=compiled.graph.graph_id,
    )

    for index, qid in enumerate(ids, 1):
        task = task_map[qid]
        plan = decomposer.decompose(
            task_id=qid,
            objective=task.problem.statement[:2000],
            metadata={"title": task.problem.title},
        )
        initial = create_artifact(
            task.problem, producer_node_id="__input__", task_id=qid
        )
        context = RunContext(
            run_id=run_id,
            task_id=qid,
            run_dir=run_dir,
            limits=config.runtime,
            semaphores=semaphores,
            contract_hash=contract_hash,
            allow_config_drift=args.allow_config_drift,
        )
        await event_writer.append(
            TelemetryEvent(
                run_id=run_id,
                task_id=qid,
                graph_id=compiled.graph.graph_id,
                event_type="task_decomposition_completed",
                metadata={
                    "decomposition_status": plan.decomposition_status.value,
                    "plan_version": plan.plan_version,
                    "subtask_count": len(plan.subtasks),
                },
            )
        )
        state, result = await runner.run(
            plan=plan,
            initial_artifacts=ArtifactBundle(slots={"problem": initial}),
            context=context,
        )
        summary = summarize_task_state(state)
        print(
            f"[{index}/{len(ids)}] {qid}: frozen={state.frozen} "
            f"status={plan.decomposition_status.value} "
            f"graph_frozen={None if result is None else result.state.frozen}"
        )
        out = run_dir / "tasks" / qid / "task_plan_result.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print(f"run_dir={run_dir}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run LCB via TaskPlan single-subtask compatibility mode"
    )
    parser.add_argument("--config", required=True)
    parser.add_argument("--graph", help="Override local_graph_template")
    parser.add_argument("--manifest")
    parser.add_argument("--output-root")
    parser.add_argument("--task-id")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--mock-llm", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--allow-config-drift", action="store_true")
    parser.add_argument("--run-id")
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
