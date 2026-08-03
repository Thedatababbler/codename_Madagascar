"""Live Codex demo through M5 Slow Loop with full process logging.

Pareto (M6) is intentionally disabled. The run dumps:
  - validated TaskPlan / decomposition
  - each subtask local subgraph (+ compiled node summary)
  - runtime events.jsonl
  - post-run Slow Loop / revision / delivery / usage summaries
  - a human-readable TRACE.md
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from orchestra.backends.factory import build_default_backend_registry
from orchestra.backends.health import healthcheck_used_backends
from orchestra.cli.validate_graph import build_compiler
from orchestra.control.fast_loop.schemas import FastLoopBudget
from orchestra.control.ready_scheduler import ReadySubtaskScheduler
from orchestra.control.single_subtask import summarize_task_state
from orchestra.control.slow_loop.schemas import SlowLoopBudget, SlowLoopConfig
from orchestra.control.task_state import TaskExecutionState
from orchestra.decomposition.decomposer import TaskDecomposer
from orchestra.decomposition.schemas import TaskPlan
from orchestra.executors.agent import AgentNodeExecutor
from orchestra.executors.harness import HarnessNodeExecutor
from orchestra.executors.registry import NodeExecutorRegistry
from orchestra.ir.artifacts import ArtifactBundle, create_artifact
from orchestra.ir.contracts import load_contracts
from orchestra.ir.graph import load_graph
from orchestra.llm.mock_async import MockAsyncLLMClient
from orchestra.runtime.backend import RunContext
from orchestra.runtime.checkpoint import CheckpointStore
from orchestra.runtime.limits import RuntimeLimits, RuntimeSemaphores
from orchestra.runtime.native_async import NativeAsyncRuntime
from orchestra.runtime.task_checkpoint import TaskCheckpointStore
from orchestra.sandbox.mock import MockSandbox
from orchestra.schemas.artifacts import ProblemArtifact
from orchestra.settings import load_env_file
from orchestra.storage.artifacts import FileArtifactStore
from orchestra.storage.events import AppendOnlyEventWriter
from orchestra.telemetry.events import TelemetryEvent


def _load_yaml(path: Path) -> dict[str, Any]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"expected mapping in {path}")
    return raw


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )


def _dump_decomposition(
    *,
    logs_dir: Path,
    plan: TaskPlan,
    plan_path: str,
) -> Path:
    deco = logs_dir / "00_decomposition"
    deco.mkdir(parents=True, exist_ok=True)
    _write_json(deco / "task_plan.json", plan.model_dump(mode="json"))
    shutil.copy2(plan_path, deco / "source_plan.yaml")
    summary = {
        "task_id": plan.task_id,
        "plan_version": plan.plan_version,
        "decomposition_status": plan.decomposition_status.value,
        "decomposition_rationale": plan.decomposition_rationale,
        "subtask_count": len(plan.subtasks),
        "subtasks": [
            {
                "subtask_id": s.subtask_id,
                "title": s.title,
                "objective": s.objective,
                "dependencies": list(s.dependencies),
                "priority": s.priority,
                "local_graph_template": s.local_graph_template,
                "keystone_harness_id": s.keystone_harness_id,
                "budget": s.budget.model_dump(mode="json"),
            }
            for s in plan.subtasks
        ],
        "communication_plan": plan.communication_plan.model_dump(mode="json"),
        "final_aggregation": plan.final_aggregation.model_dump(mode="json")
        if plan.final_aggregation
        else None,
        "pareto_enabled": False,
    }
    _write_json(deco / "decomposition_summary.json", summary)
    lines = [
        "# Task Decomposition",
        "",
        f"- task_id: `{plan.task_id}`",
        f"- status: `{plan.decomposition_status.value}`",
        f"- rationale: {plan.decomposition_rationale}",
        f"- subtasks: {len(plan.subtasks)}",
        "- pareto: **disabled** (M5 rule-based Slow Loop only)",
        "",
        "## Subtask DAG",
        "",
    ]
    for s in plan.subtasks:
        deps = ", ".join(s.dependencies) if s.dependencies else "(none)"
        lines.extend(
            [
                f"### `{s.subtask_id}` — {s.title}",
                f"- dependencies: {deps}",
                f"- graph: `{s.local_graph_template}`",
                f"- objective: {s.objective.strip()}",
                "",
            ]
        )
    (deco / "decomposition.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return deco


def _dump_subgraphs(*, logs_dir: Path, plan: TaskPlan, contracts_dir: str) -> Path:
    graphs_dir = logs_dir / "01_subgraphs"
    graphs_dir.mkdir(parents=True, exist_ok=True)
    compiler = build_compiler(contracts_dir)
    index: list[dict[str, Any]] = []
    for sub in plan.subtasks:
        graph = load_graph(sub.local_graph_template)
        compiled = compiler.compile(graph)
        sub_dir = graphs_dir / sub.subtask_id
        sub_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(sub.local_graph_template, sub_dir / "local_graph.yaml")
        _write_json(sub_dir / "graph.json", graph.model_dump(mode="json"))
        node_summary = [
            {
                "node_id": n.node_id,
                "node_kind": n.node_kind.value,
                "backend": getattr(getattr(n, "backend", None), "type", None),
                "contract_id": getattr(n, "contract_id", None),
                "harness_id": getattr(n, "harness_id", None),
                "timeout_seconds": getattr(n, "timeout_seconds", None),
            }
            for n in graph.nodes
        ]
        edge_summary = [
            {
                "edge_id": e.edge_id,
                "source_node": e.source_node,
                "destination_node": e.destination_node,
                "condition": e.condition.model_dump(mode="json") if e.condition else None,
            }
            for e in graph.edges
        ]
        payload = {
            "subtask_id": sub.subtask_id,
            "graph_id": graph.graph_id,
            "graph_hash": compiled.graph.content_hash,
            "nodes": node_summary,
            "edges": edge_summary,
            "final_output_slot": graph.final_output_slot,
        }
        _write_json(sub_dir / "compiled_summary.json", payload)
        md = [
            f"# Subgraph for `{sub.subtask_id}`",
            "",
            f"- template: `{sub.local_graph_template}`",
            f"- graph_id: `{graph.graph_id}`",
            f"- graph_hash: `{compiled.graph.content_hash}`",
            "",
            "## Nodes",
            "",
        ]
        for n in node_summary:
            md.append(
                f"- `{n['node_id']}` ({n['node_kind']})"
                + (f" backend={n['backend']}" if n["backend"] else "")
                + (f" harness={n['harness_id']}" if n["harness_id"] else "")
            )
        md.extend(["", "## Edges", ""])
        for e in edge_summary:
            md.append(f"- `{e['source_node']}` → `{e['destination_node']}` ({e['edge_id']})")
        (sub_dir / "subgraph.md").write_text("\n".join(md) + "\n", encoding="utf-8")
        index.append(payload)
    _write_json(graphs_dir / "index.json", index)
    return graphs_dir


def _dump_post_run(*, logs_dir: Path, state: TaskExecutionState, run_dir: Path) -> None:
    post = logs_dir / "03_post_run"
    post.mkdir(parents=True, exist_ok=True)
    _write_json(post / "task_state_summary.json", summarize_task_state(state))
    _write_json(
        post / "slow_loop_state.json",
        state.slow_loop_state.model_dump(mode="json")
        if state.slow_loop_state is not None
        else None,
    )
    _write_json(
        post / "slow_loop_history.json",
        [x.model_dump(mode="json") for x in (state.slow_loop_history or [])],
    )
    _write_json(
        post / "plan_revision_history.json",
        [x.model_dump(mode="json") for x in (state.plan_revision_history or [])],
    )
    _write_json(
        post / "delivery_ledger.json",
        [x.model_dump(mode="json") for x in (state.delivery_ledger or [])],
    )
    _write_json(
        post / "backend_usage_records.json",
        [x.model_dump(mode="json") for x in (state.backend_usage_records or [])],
    )
    _write_json(
        post / "workspace_commit_records.json",
        [x.model_dump(mode="json") for x in (state.workspace_commit_records or [])],
    )
    _write_json(
        post / "pareto_state.json",
        state.pareto_state.model_dump(mode="json")
        if state.pareto_state is not None
        else {"enabled": False, "note": "Pareto intentionally disabled for this demo"},
    )
    # Copy events into logs for convenience.
    events = run_dir / "events.jsonl"
    if events.exists():
        shutil.copy2(events, logs_dir / "02_runtime" / "events.jsonl")


def _write_trace_md(
    *,
    run_dir: Path,
    plan: TaskPlan,
    state: TaskExecutionState | None,
    summary: dict[str, Any],
) -> None:
    lines = [
        "# M5 Codex Process Trace",
        "",
        f"- run_dir: `{run_dir}`",
        f"- task_id: `{plan.task_id}`",
        "- pareto: disabled",
        "- slow_loop: enabled (rule-based)",
        f"- frozen: {summary.get('frozen')}",
        f"- latency_ms: {summary.get('latency_ms')}",
        "",
        "## Where to look",
        "",
        "1. `logs/00_decomposition/` — task split + communication plan",
        "2. `logs/01_subgraphs/<subtask_id>/` — each step's local graph",
        "3. `logs/02_runtime/events.jsonl` — runtime telemetry",
        "4. `logs/03_post_run/` — Slow Loop / revisions / usage / delivery",
        "",
        "## Subtask outcomes",
        "",
    ]
    if state is not None:
        for sid, sub in state.subtasks.items():
            lines.append(
                f"- `{sid}`: status=`{sub.status.value}` "
                f"attempts={len(sub.attempts)} "
                f"workspace={sub.workspace_ref or '-'}"
            )
        lines.extend(
            [
                "",
                f"- active_plan_revision_id: `{state.active_plan_revision_id}`",
                (
                    "- slow_loop updates: "
                    + str(
                        getattr(state.slow_loop_state, "updates_applied", 0)
                        if state.slow_loop_state
                        else 0
                    )
                ),
                f"- delivery records: {len(state.delivery_ledger or [])}",
                f"- usage records: {len(state.backend_usage_records or [])}",
            ]
        )
    (run_dir / "TRACE.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


async def _run(args: argparse.Namespace) -> int:
    load_env_file()
    started_at = datetime.now(UTC)
    wall_started = time.perf_counter()
    config_path = Path(args.config)
    config = _load_yaml(config_path)
    experiment = config["experiment"]
    if bool((config.get("pareto") or {}).get("enabled", False)):
        raise SystemExit("This demo requires pareto.enabled=false")

    source_repo = args.source_repo or experiment["source_repo"]
    plan_path = args.plan or experiment["plan_config"]
    contracts_dir = experiment.get("contracts_dir", "configs/contracts")
    output_root = Path(
        args.output_root or experiment.get("output_root", "outputs/m5_codex_process_demo")
    )
    graph_default = experiment.get(
        "graph_config", "configs/graphs/codex_single_implementer.yaml"
    )

    contracts = load_contracts(contracts_dir)
    contract_hash = hashlib.sha256(
        "".join(contracts[key].model_dump_json() for key in sorted(contracts)).encode()
    ).hexdigest()
    candidate = TaskPlan.model_validate(_load_yaml(Path(plan_path)))
    decomposer = TaskDecomposer(
        enabled=True,
        default_graph_template=graph_default,
        keystone_harness_id="repository_test_harness",
        require_graph_files=True,
    )
    plan = decomposer.decompose(
        task_id=candidate.task_id,
        objective=candidate.subtasks[0].objective,
        candidate_plan=candidate,
        metadata={"plan_config": plan_path, "demo": "m5_codex_process"},
    )

    run_id = args.run_id or f"m5_codex_demo-{started_at.strftime('%Y%m%d%H%M%S')}"
    # Absolute run_dir is required: git worktree add resolves relative paths
    # against the source repo cwd and would otherwise nest candidates inside it.
    run_dir = (output_root / run_id).resolve()
    logs_dir = run_dir / "logs"
    (logs_dir / "02_runtime").mkdir(parents=True, exist_ok=True)
    run_dir.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=getattr(logging, str((config.get("logging") or {}).get("level", "INFO"))),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(logs_dir / "02_runtime" / "python.log", encoding="utf-8"),
        ],
    )
    logger = logging.getLogger("m5_codex_demo")
    logger.info("run_dir=%s pareto=disabled", run_dir)

    deco_dir = _dump_decomposition(logs_dir=logs_dir, plan=plan, plan_path=plan_path)
    graphs_dir = _dump_subgraphs(
        logs_dir=logs_dir, plan=plan, contracts_dir=contracts_dir
    )
    _write_json(
        logs_dir / "02_runtime" / "run_config.json",
        {
            "config_path": str(config_path),
            "plan_path": plan_path,
            "source_repo": source_repo,
            "pareto_enabled": False,
            "slow_loop": config.get("slow_loop"),
            "fast_loop": config.get("fast_loop"),
            "env_model": os.getenv("CODEX_MODEL"),
        },
    )

    if args.dry_run:
        summary = {
            "mode": "m5_codex_process_demo",
            "dry_run": True,
            "task_id": plan.task_id,
            "subtasks": [s.subtask_id for s in plan.subtasks],
            "decomposition_dir": str(deco_dir),
            "subgraphs_dir": str(graphs_dir),
            "run_dir": str(run_dir),
            "pareto_enabled": False,
        }
        _write_json(run_dir / "summary.json", summary)
        _write_trace_md(run_dir=run_dir, plan=plan, state=None, summary=summary)
        print(json.dumps(summary, indent=2))
        print(f"run_dir={run_dir}")
        return 0

    llm = MockAsyncLLMClient({})
    backend_registry = build_default_backend_registry(
        llm, include_smolagents=False, include_codex=True
    )
    if not backend_registry.has("codex_sdk"):
        raise RuntimeError(
            "codex_sdk unavailable; install with: uv sync --extra codex"
        )

    runtime_cfg = config.get("runtime") or {}
    limits = RuntimeLimits(
        max_parallel_benchmark_tasks=int(runtime_cfg.get("max_parallel_benchmark_tasks", 1)),
        max_parallel_nodes_per_task=int(runtime_cfg.get("max_parallel_nodes_per_task", 2)),
        max_parallel_llm_calls=int(runtime_cfg.get("max_parallel_llm_calls", 2)),
        max_parallel_sandboxes=int(runtime_cfg.get("max_parallel_sandboxes", 1)),
    )
    artifact_store = FileArtifactStore(run_dir)
    checkpoint_store = CheckpointStore(run_dir)
    task_checkpoint_store = TaskCheckpointStore(run_dir)
    event_writer = AppendOnlyEventWriter(run_dir)
    runtime = NativeAsyncRuntime(
        executors=NodeExecutorRegistry(
            agent_executor=AgentNodeExecutor(contracts, backend_registry),
            harness_executor=HarnessNodeExecutor(MockSandbox(), timeout_seconds=60),
        ),
        artifact_store=artifact_store,
        checkpoint_store=checkpoint_store,
        event_writer=event_writer,
    )

    # Healthcheck against the first subtask graph.
    first_graph = load_graph(plan.subtasks[0].local_graph_template)
    compiled = build_compiler(contracts_dir).compile(first_graph)
    await healthcheck_used_backends(
        registry=backend_registry,
        graph=compiled,
        event_writer=event_writer,
        run_id=run_id,
        graph_id=compiled.graph.graph_id,
    )

    slow_cfg_raw = config.get("slow_loop") or {}
    slow_budget_raw = {
        k: v
        for k, v in slow_cfg_raw.items()
        if k
        in {
            "max_updates_per_task",
            "max_candidates_per_update",
            "max_wall_time_seconds",
            "min_commits_between_updates",
            "failure_policy",
            "context_pressure_ratio",
            "budget_pressure_ratio",
            "repeated_failure_threshold",
        }
    }
    slow_loop_config = SlowLoopConfig(
        enabled=bool(slow_cfg_raw.get("enabled", True)),
        budget=SlowLoopBudget(**slow_budget_raw),
        allowed_backend_assignments=dict(
            slow_cfg_raw.get("allowed_backend_assignments") or {"coding": ["codex_sdk"]}
        ),
        backend_model_pools=dict(slow_cfg_raw.get("backend_model_pools") or {}),
    )
    # Resolve ${ENV} style model pool entries if present.
    resolved_pools: dict[str, list[str]] = {}
    for backend_id, models in slow_loop_config.backend_model_pools.items():
        resolved = []
        for model in models:
            text = str(model)
            if text.startswith("${") and text.endswith("}"):
                inner = text[2:-1]
                env_name, _, default = inner.partition(":-")
                resolved.append(os.getenv(env_name, default or env_name))
            else:
                resolved.append(text)
        resolved_pools[backend_id] = resolved
    slow_loop_config.backend_model_pools = resolved_pools

    fast_cfg = config.get("fast_loop") or {}
    fast_budget = FastLoopBudget(
        max_candidates=int(fast_cfg.get("max_candidates", 2)),
        max_total_backend_calls=int(fast_cfg.get("max_total_backend_calls", 6)),
    )

    # candidate_policy=None → M5 rule-based Slow Loop; Pareto stays off.
    scheduler = ReadySubtaskScheduler(
        runtime=runtime,
        artifact_store=artifact_store,
        task_checkpoint_store=task_checkpoint_store,
        contracts_dir=contracts_dir,
        source_repo=str(Path(source_repo).resolve()),
        budget=fast_budget,
        slow_loop_config=slow_loop_config,
        max_concurrent_subtasks=1,
    )

    problem = ProblemArtifact(
        question_id=plan.task_id,
        title="Codex tiny repo M5 demo",
        statement=(
            "Fix the repository so that all tests pass. "
            "Make the smallest correct change. "
            "Run the tests before finishing. Do not create subagents."
        ),
        difficulty="easy",
        platform="fixture",
    )
    initial = create_artifact(
        problem, producer_node_id="__input__", task_id=plan.task_id
    )
    context = RunContext(
        run_id=run_id,
        task_id=plan.task_id,
        run_dir=run_dir,
        limits=limits,
        semaphores=RuntimeSemaphores(limits),
        contract_hash=contract_hash,
        allow_config_drift=args.allow_config_drift,
    )
    state = TaskExecutionState.from_plan(
        plan, artifact_store_ref=str(run_dir / "artifacts")
    )
    # Hard guarantee: no Pareto activation on this demo path.
    state.pareto_state = None

    await event_writer.append(
        TelemetryEvent(
            run_id=run_id,
            task_id=plan.task_id,
            graph_id=compiled.graph.graph_id,
            event_type="task_decomposition_completed",
            metadata={
                "decomposition_status": plan.decomposition_status.value,
                "plan_version": plan.plan_version,
                "subtask_ids": [s.subtask_id for s in plan.subtasks],
                "pareto_enabled": False,
                "logs_decomposition": str(deco_dir),
                "logs_subgraphs": str(graphs_dir),
            },
        )
    )

    error: str | None = None
    try:
        logger.info("starting ReadySubtaskScheduler.run_task (M5, no Pareto)")
        state = await scheduler.run_task(
            plan,
            state,
            initial_artifacts=ArtifactBundle(slots={"problem": initial}),
            context=context,
            source_repo=str(Path(source_repo).resolve()),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("demo failed")
        error = f"{type(exc).__name__}: {exc}"

    latency_ms = int((time.perf_counter() - wall_started) * 1000)
    _dump_post_run(logs_dir=logs_dir, state=state, run_dir=run_dir)
    summary = {
        "mode": "m5_codex_process_demo",
        "task_id": plan.task_id,
        "run_dir": str(run_dir),
        "frozen": bool(state.frozen),
        "error": error,
        "latency_ms": latency_ms,
        "pareto_enabled": False,
        "slow_loop_enabled": slow_loop_config.enabled,
        "subtask_status": {
            sid: sub.status.value for sid, sub in state.subtasks.items()
        },
        "active_plan_revision_id": state.active_plan_revision_id,
        "slow_loop_updates": getattr(state.slow_loop_state, "updates_applied", 0)
        if state.slow_loop_state
        else 0,
        "delivery_records": len(state.delivery_ledger or []),
        "usage_records": len(state.backend_usage_records or []),
        "logs": {
            "decomposition": str(deco_dir),
            "subgraphs": str(graphs_dir),
            "runtime": str(logs_dir / "02_runtime"),
            "post_run": str(logs_dir / "03_post_run"),
            "trace_md": str(run_dir / "TRACE.md"),
        },
    }
    _write_json(run_dir / "summary.json", summary)
    _write_trace_md(run_dir=run_dir, plan=plan, state=state, summary=summary)
    print(json.dumps(summary, indent=2, sort_keys=True))
    print(f"run_dir={run_dir}")
    print(f"TRACE={run_dir / 'TRACE.md'}")
    if error:
        return 1
    return 0 if state.frozen else 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default="configs/experiments/m5_codex_process_demo.yaml",
    )
    parser.add_argument("--plan")
    parser.add_argument("--source-repo")
    parser.add_argument("--output-root")
    parser.add_argument("--run-id")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--allow-config-drift", action="store_true")
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
