"""Formal LiveCodeBench abc309_a × 3 Codex subtasks (public-pytest repo fixture).

Loads the real LCB ProblemArtifact, drives ReadySubtaskScheduler with codex_sdk
graphs (analyze → implement → verify), and dumps the same process logs as the
M5 Codex demo. Pareto stays off.

Note: existing smolagents MAS graphs (lcb_mas_*.yaml) cannot use codex_sdk
directly; this path uses Codex-compatible repo graphs + public examples as
pytest. Private LCB tests are not exposed to the agent.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from orchestra.adapters.livecodebench.loader import LiveCodeBenchLoader
from orchestra.backends.factory import resolve_backend_registry
from orchestra.backends.health import healthcheck_used_backends
from orchestra.cli.run_m5_codex_demo import (
    _dump_decomposition,
    _dump_post_run,
    _dump_subgraphs,
    _write_json,
    _write_trace_md,
)
from orchestra.cli.validate_graph import build_compiler
from orchestra.control.fast_loop.schemas import FastLoopBudget
from orchestra.control.ready_scheduler import ReadySubtaskScheduler
from orchestra.control.slow_loop.controller import SlowLoopController
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
from orchestra.llm.openai_compatible_async import OpenAICompatibleAsyncClient
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


def _load_synthetic_problem(config: dict[str, Any]) -> ProblemArtifact:
    bench = config.get("benchmark") or {}
    path = Path(
        bench.get("synthetic_problem")
        or "tests/fixtures/lcb_abc309_a_synthetic_problem.json"
    )
    if not path.is_absolute():
        path = Path.cwd() / path
    if not path.exists():
        raise RuntimeError(
            f"synthetic ProblemArtifact fixture missing: {path}. "
            "Provide --synthetic-problem or restore the fixture file."
        )
    return ProblemArtifact.model_validate(json.loads(path.read_text(encoding="utf-8")))


def _load_lcb_problem(config: dict[str, Any]) -> ProblemArtifact:
    bench = config.get("benchmark") or {}
    data_dir = bench.get("data_dir") or os.environ.get("LCB_DATA_DIR")
    if not data_dir:
        raise RuntimeError(
            "Real LCB dataset path missing. Set benchmark.data_dir / LCB_DATA_DIR, "
            "or pass --synthetic-problem for the API-free CI fixture. "
            "Refusing silent fallback to synthetic data."
        )
    data_path = Path(str(data_dir))
    if not data_path.exists():
        raise RuntimeError(
            f"Real LCB dataset path does not exist: {data_path}. "
            "Install/mount the LiveCodeBench release, or pass --synthetic-problem "
            "for the API-free CI fixture. Refusing silent fallback to synthetic data."
        )
    release = str(bench.get("release_version") or "release_v6")
    task_id = str(bench.get("task_id") or "abc309_a")
    loader = LiveCodeBenchLoader(data_dir=str(data_path), release_version=release)
    tasks = loader.load(task_ids={task_id})
    if not tasks:
        raise RuntimeError(f"LCB task not found: {task_id} under {data_path}")
    return tasks[0].problem


async def _run(args: argparse.Namespace) -> int:
    load_env_file()
    started_at = datetime.now(UTC)
    wall_started = time.perf_counter()

    config_path = Path(args.config)
    config = _load_yaml(config_path)
    experiment = config["experiment"]
    if bool((config.get("pareto") or {}).get("enabled", False)):
        raise SystemExit("This run requires pareto.enabled=false")

    source_repo = args.source_repo or experiment["source_repo"]
    plan_path = args.plan or experiment["plan_config"]
    contracts_dir = experiment.get("contracts_dir", "configs/contracts")
    output_root = Path(
        args.output_root
        or experiment.get("output_root", "outputs/lcb_formal_codex_three_subtask")
    )
    graph_default = experiment.get(
        "graph_config", "configs/graphs/codex_lcb_analyze.yaml"
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
        metadata={"plan_config": plan_path, "demo": "lcb_formal_codex_three_subtask"},
    )

    use_synthetic = bool(args.synthetic_problem)
    if use_synthetic:
        problem = _load_synthetic_problem(config)
        problem_source = "synthetic_fixture"
    else:
        problem = _load_lcb_problem(config)
        problem_source = "real_lcb"

    run_id = args.run_id or f"lcb_codex-{started_at.strftime('%Y%m%d%H%M%S')}"
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
    logger = logging.getLogger("lcb_codex_three_subtask")
    mock_backends = bool(args.mock_backends or args.mock_llm)
    logger.info(
        "run_dir=%s task=%s question_id=%s problem_source=%s mock_backends=%s "
        "pareto=disabled sandbox_override=%s",
        run_dir,
        plan.task_id,
        problem.question_id,
        problem_source,
        mock_backends,
        os.getenv("ADAMAS_CODEX_SANDBOX_OVERRIDE"),
    )

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
            "evaluation": config.get("evaluation"),
            "sandbox": config.get("sandbox"),
            "mock_backends": mock_backends,
            "problem_source": problem_source,
            "env_model": os.getenv("CODEX_MODEL"),
            "lcb": {
                "question_id": problem.question_id,
                "title": problem.title,
                "difficulty": problem.difficulty,
                "platform": problem.platform,
                "public_examples": len(problem.public_examples),
            },
        },
    )
    _write_json(
        logs_dir / "00_decomposition" / "problem_artifact.json",
        problem.model_dump(mode="json"),
    )

    if args.dry_run:
        summary = {
            "mode": "lcb_formal_codex_three_subtask",
            "dry_run": True,
            "task_id": plan.task_id,
            "question_id": problem.question_id,
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

    if mock_backends:
        backend_registry, backend_manifest = resolve_backend_registry(
            mock_backends=True,
            include_smolagents=False,
            include_codex=True,
        )
    else:
        llm = OpenAICompatibleAsyncClient()
        backend_registry, backend_manifest = resolve_backend_registry(
            mock_backends=False,
            client=llm,
            include_smolagents=False,
            include_codex=True,
        )
        if not backend_registry.has("codex_sdk"):
            raise RuntimeError(
                "codex_sdk unavailable; install with: uv sync --extra codex "
                "or pass --mock-backends for API-free validation"
            )

    runtime_cfg = config.get("runtime") or {}
    runtime_cap = max(1, int(runtime_cfg.get("max_concurrent_subtasks", 1)))
    limits = RuntimeLimits(
        max_parallel_benchmark_tasks=int(runtime_cfg.get("max_parallel_benchmark_tasks", 1)),
        max_parallel_nodes_per_task=int(runtime_cfg.get("max_parallel_nodes_per_task", 2)),
        max_parallel_llm_calls=int(runtime_cfg.get("max_parallel_llm_calls", 2)),
        max_parallel_sandboxes=int(runtime_cfg.get("max_parallel_sandboxes", 1)),
        max_concurrent_subtasks=runtime_cap,
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

    slow_loop = SlowLoopController(
        config=slow_loop_config,
        checkpoint_store=task_checkpoint_store,
    )
    scheduler = ReadySubtaskScheduler(
        runtime=runtime,
        artifact_store=artifact_store,
        task_checkpoint_store=task_checkpoint_store,
        contracts_dir=contracts_dir,
        source_repo=str(Path(source_repo).resolve()),
        budget=fast_budget,
        slow_loop=slow_loop,
        slow_loop_config=slow_loop_config,
        max_concurrent_subtasks=runtime_cap,
        allow_concurrent_subtasks=runtime_cap > 1,
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
                "question_id": problem.question_id,
                "logs_decomposition": str(deco_dir),
                "logs_subgraphs": str(graphs_dir),
            },
        )
    )

    error: str | None = None
    try:
        logger.info("starting ReadySubtaskScheduler.run_task (LCB+Codex, no Pareto)")
        state = await scheduler.run_task(
            plan,
            state,
            initial_artifacts=ArtifactBundle(slots={"problem": initial}),
            context=context,
            source_repo=str(Path(source_repo).resolve()),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("lcb codex run failed")
        error = f"{type(exc).__name__}: {exc}"

    latency_ms = int((time.perf_counter() - wall_started) * 1000)
    _dump_post_run(logs_dir=logs_dir, state=state, run_dir=run_dir)

    canonical = run_dir / "tasks" / plan.task_id / "canonical" / "repo" / "solution.py"
    solution_text = (
        canonical.read_text(encoding="utf-8") if canonical.exists() else None
    )
    summary = {
        "mode": "lcb_formal_codex_three_subtask",
        "task_id": plan.task_id,
        "question_id": problem.question_id,
        "title": problem.title,
        "run_dir": str(run_dir),
        "frozen": bool(state.frozen),
        "error": error,
        "latency_ms": latency_ms,
        "pareto_enabled": False,
        "slow_loop_enabled": slow_loop_config.enabled,
        "problem_source": problem_source,
        "mock_backends": mock_backends,
        "runtime_concurrency_cap": runtime_cap,
        "backend_override": backend_manifest.get("backend_override"),
        "subtask_status": {
            sid: sub.status.value for sid, sub in state.subtasks.items()
        },
        "committed": [
            sid
            for sid, sub in state.subtasks.items()
            if sub.status.value == "committed"
        ],
        "active_plan_revision_id": state.active_plan_revision_id,
        "m5_revision_count": len(state.plan_revision_history or []),
        "slow_loop_updates": getattr(state.slow_loop_state, "updates_applied", 0)
        if state.slow_loop_state
        else 0,
        "delivery_records": len(state.delivery_ledger or []),
        "usage_records": len(state.backend_usage_records or []),
        "canonical_solution_path": str(canonical) if canonical.exists() else None,
        "canonical_solution_preview": (solution_text or "")[:2000] or None,
        "logs": {
            "decomposition": str(deco_dir),
            "subgraphs": str(graphs_dir),
            "runtime": str(logs_dir / "02_runtime"),
            "post_run": str(logs_dir / "03_post_run"),
            "trace_md": str(run_dir / "TRACE.md"),
        },
    }
    _write_json(run_dir / "summary.json", summary)
    _write_json(
        run_dir / "run_manifest.json",
        {
            "runner": "run_lcb_codex_three_subtask",
            "started_at": started_at.isoformat(),
            "config_path": str(config_path),
            "problem_source": problem_source,
            **backend_manifest,
            "runtime_concurrency_cap": runtime_cap,
            "slow_loop_enabled": slow_loop_config.enabled,
            "evaluation": config.get("evaluation"),
        },
    )
    _write_trace_md(run_dir=run_dir, plan=plan, state=state, summary=summary)
    # Rewrite TRACE header for this mode.
    trace = (run_dir / "TRACE.md").read_text(encoding="utf-8")
    trace = trace.replace(
        "# M5 Codex Process Trace",
        "# LCB Formal Codex Three-Subtask Trace\n\n"
        f"- question_id: `{problem.question_id}` ({problem.title})",
        1,
    )
    (run_dir / "TRACE.md").write_text(trace, encoding="utf-8")

    print(json.dumps(summary, indent=2, sort_keys=True, ensure_ascii=False))
    print(f"run_dir={run_dir}")
    print(f"TRACE={run_dir / 'TRACE.md'}")
    if canonical.exists():
        print(f"canonical_solution={canonical}")
    if error:
        return 1
    return 0 if state.frozen else 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default="configs/experiments/lcb_formal_codex_three_subtask.yaml",
    )
    parser.add_argument("--plan")
    parser.add_argument("--source-repo")
    parser.add_argument("--output-root")
    parser.add_argument("--run-id")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--allow-config-drift", action="store_true")
    parser.add_argument(
        "--mock-backends",
        action="store_true",
        help="Fully API-free deterministic backends (no Codex/OpenAI clients).",
    )
    parser.add_argument(
        "--mock-llm",
        action="store_true",
        help="Compatibility alias for --mock-backends.",
    )
    parser.add_argument(
        "--synthetic-problem",
        action="store_true",
        help="Use the API-free synthetic ProblemArtifact fixture (CI).",
    )
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
