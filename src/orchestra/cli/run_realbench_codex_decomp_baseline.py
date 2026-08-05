"""RealBench AdaMAS: dynamic TaskPlan + public harness graphs + scheduler.

Builds a milestone DAG from public_design, binds each milestone to
``repository_test_harness`` with role-graded ``scripts/adamas_public_check.py``,
and executes via ``ReadySubtaskScheduler``. One-shot only (no fast/slow loop).
Hidden RealBench tests stay offline (see ``eval_realbench_codex_decomp_baseline``).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from orchestra.backends.factory import resolve_backend_registry
from orchestra.backends.health import healthcheck_used_backends
from orchestra.cli.run_m5_codex_demo import (
    _dump_decomposition,
    _dump_post_run,
    _dump_subgraphs,
    _write_trace_md,
)
from orchestra.cli.validate_graph import build_compiler
from orchestra.control.fast_loop.schemas import FastLoopBudget
from orchestra.control.ready_scheduler import ReadySubtaskScheduler
from orchestra.control.slow_loop.controller import SlowLoopController
from orchestra.control.slow_loop.schemas import SlowLoopBudget, SlowLoopConfig
from orchestra.control.task_state import TaskExecutionState
from orchestra.decomposition.decomposer import TaskDecomposer
from orchestra.decomposition.realbench_plan import (
    DEFAULT_GRAPH_CATALOG,
    build_realbench_candidate_plan,
)
from orchestra.decomposition.schemas import DecompositionLimits, TaskPlan
from orchestra.executors.agent import AgentNodeExecutor
from orchestra.executors.harness import HarnessNodeExecutor
from orchestra.executors.registry import NodeExecutorRegistry
from orchestra.ir.artifacts import ArtifactBundle, create_artifact
from orchestra.ir.contracts import load_contracts
from orchestra.ir.graph import load_graph
from orchestra.llm.openai_compatible_async import OpenAICompatibleAsyncClient
from orchestra.realbench.public_harness import materialize_public_harness
from orchestra.runtime.backend import RunContext
from orchestra.runtime.checkpoint import CheckpointStore
from orchestra.runtime.limits import RuntimeLimits, RuntimeSemaphores
from orchestra.runtime.native_async import NativeAsyncRuntime
from orchestra.runtime.task_checkpoint import TaskCheckpointStore
from orchestra.sandbox.mock import MockSandbox
from orchestra.schemas.artifacts import ProblemArtifact
from orchestra.settings import load_env_file, resolve_runtime_settings
from orchestra.storage.artifacts import FileArtifactStore
from orchestra.storage.events import AppendOnlyEventWriter
from orchestra.telemetry.events import TelemetryEvent

TASKS: list[dict[str, str]] = [
    {
        "task_id": "encore-ecosystem_NodeFlow",
        "domain": "System",
        "domain_dir": "System_repos",
    },
    {
        "task_id": "AlienMajik_SnoopR",
        "domain": "Security",
        "domain_dir": "Security_repos",
    },
    {
        "task_id": "benbovy_xproj",
        "domain": "System",
        "domain_dir": "System_repos",
    },
    {
        "task_id": "FreddyRodgers_emojichef",
        "domain": "Text_Processing",
        "domain_dir": "Text_Processing_repos",
    },
    {
        "task_id": "dkweiss31_floquet",
        "domain": "System",
        "domain_dir": "System_repos",
    },
]


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


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _load_descriptions(level2_root: Path) -> dict[str, str]:
    path = level2_root / "des.jsonl"
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)
        out[str(obj["proj_name"])] = str(obj["description"])
    return out


def _find_class_uml(uml_dir: Path, task_id: str) -> Path | None:
    cand = uml_dir / f"{task_id}_uml.json"
    if cand.exists():
        return cand
    matches = sorted(uml_dir.glob("*_uml.json"))
    return matches[0] if matches else None


def _materialize_empty_tree(tree_text: str, dest: Path) -> None:
    path_stack: list[str] = []
    for raw in tree_text.splitlines():
        line = raw.rstrip("\n")
        if not line.strip():
            continue
        match = re.match(r"^([\s│]*)(?:├──|└──)?\s*(.*)$", line)
        if not match:
            continue
        prefix, name = match.group(1), match.group(2).strip()
        if not name or name in {"│", "├──", "└──"}:
            continue
        depth = len(re.findall(r"(?: {4}|│   )", prefix))
        is_dir = name.endswith("/")
        name = name.rstrip("/")
        if depth == 0 and name in {"proj_clean", "proj_with_test", "."}:
            path_stack = []
            continue
        path_stack = path_stack[:depth]
        path_stack.append(name)
        rel = Path(*path_stack)
        target = dest / rel
        if is_dir or ("." not in name):
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                target.write_text("", encoding="utf-8")


def build_public_workspace(
    *,
    level2_root: Path,
    task: dict[str, str],
    workspaces_root: Path,
) -> Path:
    task_id = task["task_id"]
    src = level2_root / task["domain_dir"] / task_id
    uml_src = src / "uml_output"
    if not uml_src.is_dir():
        raise FileNotFoundError(f"missing uml_output for {task_id}: {uml_src}")
    ws = workspaces_root / task_id
    if ws.exists():
        shutil.rmtree(ws)
    ws.mkdir(parents=True, exist_ok=True)

    public = ws / "public_design"
    public.mkdir()
    for name in ("package.json", "tree.txt", "dag.json"):
        path = uml_src / name
        if path.exists():
            shutil.copy2(path, public / name)
    class_uml = _find_class_uml(uml_src, task_id)
    if class_uml is not None:
        shutil.copy2(class_uml, public / class_uml.name)
    for img in sorted(uml_src.glob("UMLClassDiagram_*.jpg")):
        shutil.copy2(img, public / img.name)

    descriptions = _load_descriptions(level2_root)
    req = descriptions.get(task_id, "")
    (ws / "REQUIREMENTS.md").write_text(req, encoding="utf-8")

    tree_path = uml_src / "tree.txt"
    if tree_path.exists():
        _materialize_empty_tree(
            tree_path.read_text(encoding="utf-8", errors="ignore"), ws
        )

    (ws / "TASK.md").write_text(
        f"""# TASK.md

## Project
- task_id: `{task_id}`
- domain: `{task["domain"]}`

## Goal
Implement the complete Python project described by the public requirements and
UML design files in this repository.

## Public inputs
- `REQUIREMENTS.md`
- `public_design/` (package/class UML, tree, dag)
- Empty scaffold paths derived from `tree.txt` (no reference implementation)

## Constraints
- Hidden evaluation tests and reference implementations are not available.
- Do not search outside this repository for answers.
- Do not create Codex subagents; implement directly in this workspace.
""",
        encoding="utf-8",
    )
    (ws / "README.md").write_text(
        f"# {task_id}\n\nSee TASK.md and public_design/.\n",
        encoding="utf-8",
    )
    # Trusted marker + AdaMAS-owned public contract harness (no hidden tests).
    (ws / ".adamas_trusted_harness").write_text("trusted_fixture\n", encoding="utf-8")
    materialize_public_harness(ws)

    subprocess.run(["git", "init"], cwd=str(ws), check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(ws), "config", "user.email", "bench@local"], check=True
    )
    subprocess.run(
        ["git", "-C", str(ws), "config", "user.name", "AdaMAS RealBench Baseline"],
        check=True,
    )
    subprocess.run(["git", "-C", str(ws), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(ws), "commit", "-m", "pristine public workspace"],
        check=True,
        capture_output=True,
    )
    return ws


def build_dynamic_task_plan(
    task_id: str,
    *,
    workspace: Path,
    plan_path: Path,
    experiment: dict[str, Any],
) -> TaskPlan:
    """Build + validate a dynamic milestone TaskPlan for one RealBench task."""
    deco_cfg = dict(experiment.get("decomposition") or {})
    catalog = dict(deco_cfg.get("graph_catalog") or DEFAULT_GRAPH_CATALOG)
    candidate_payload = build_realbench_candidate_plan(
        task_id=task_id,
        workspace=workspace,
        graph_catalog=catalog,
        max_implementation_milestones=int(
            deco_cfg.get("max_implementation_milestones", 2)
        ),
    )
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(
        yaml.safe_dump(candidate_payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    limits = DecompositionLimits(
        min_subtasks=int(deco_cfg.get("min_subtasks", 2)),
        max_subtasks=int(deco_cfg.get("max_subtasks", 6)),
        max_dependency_depth=int(deco_cfg.get("max_dependency_depth", 6)),
    )
    decomposer = TaskDecomposer(
        enabled=True,
        default_graph_template=catalog.get(
            "integration", DEFAULT_GRAPH_CATALOG["integration"]
        ),
        keystone_harness_id="repository_test_harness",
        limits=limits,
        require_graph_files=True,
        require_public_keystone_harness=bool(
            deco_cfg.get("require_public_keystone_harness", True)
        ),
    )
    return decomposer.decompose(
        task_id=candidate_payload["task_id"],
        objective=candidate_payload["subtasks"][0]["objective"],
        candidate_plan=candidate_payload,
        metadata={
            "plan_config": str(plan_path),
            "demo": "realbench_dynamic_taskplan_public_harness",
            "realbench_task_id": task_id,
            "plan_builder": "build_realbench_candidate_plan",
        },
    )


def _build_problem(task: dict[str, str], requirements: str) -> ProblemArtifact:
    return ProblemArtifact(
        question_id=task["task_id"],
        title=task["task_id"],
        statement=(
            f"RealBench task `{task['task_id']}` ({task['domain']}).\n\n"
            "Implement the complete Python repository described by TASK.md, "
            "REQUIREMENTS.md, and public_design/ in the workspace.\n\n"
            f"{requirements[:12000]}"
        ),
        difficulty="level2",
        platform="realbench",
        starter_code="",
        public_examples=[],
    )


async def _run_one(
    *,
    task: dict[str, str],
    config: dict[str, Any],
    config_path: Path,
    batch_dir: Path,
    dry_run: bool,
    allow_config_drift: bool,
) -> dict[str, Any]:
    started_at = datetime.now(UTC)
    wall_started = time.perf_counter()
    experiment = config["experiment"]
    level2_root = Path(
        experiment.get("dataset_root")
        or "data/realbench/datasets/projs_filtered_uml/level2"
    )
    if not level2_root.is_absolute():
        level2_root = _repo_root() / level2_root
    contracts_dir = experiment.get("contracts_dir", "configs/contracts")
    task_id = task["task_id"]
    run_dir = batch_dir / task_id
    logs_dir = run_dir / "logs"
    (logs_dir / "02_runtime").mkdir(parents=True, exist_ok=True)
    workspaces_root = batch_dir / "workspaces"
    source_repo = build_public_workspace(
        level2_root=level2_root,
        task=task,
        workspaces_root=workspaces_root,
    )
    plan_path = run_dir / "plan.yaml"
    plan = build_dynamic_task_plan(
        task_id,
        workspace=source_repo,
        plan_path=plan_path,
        experiment=experiment,
    )
    requirements = (source_repo / "REQUIREMENTS.md").read_text(encoding="utf-8")
    problem = _build_problem(task, requirements)

    logging.basicConfig(
        level=getattr(logging, str((config.get("logging") or {}).get("level", "INFO"))),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(
                logs_dir / "02_runtime" / "python.log", encoding="utf-8"
            ),
        ],
        force=True,
    )
    logger = logging.getLogger("realbench_codex_decomp_baseline")
    logger.info(
        "task=%s run_dir=%s source_repo=%s subtasks=%s "
        "slow_loop=off fast_loop=off fresh_thread=true public_keystone=on",
        task_id,
        run_dir,
        source_repo,
        [s.subtask_id for s in plan.subtasks],
    )

    deco_dir = _dump_decomposition(
        logs_dir=logs_dir, plan=plan, plan_path=str(plan_path)
    )
    if (source_repo / "adamas_public_harness.json").is_file():
        _write_json(
            Path(deco_dir) / "public_harness_manifest.json",
            json.loads(
                (source_repo / "adamas_public_harness.json").read_text(encoding="utf-8")
            ),
        )
    graphs_dir = _dump_subgraphs(
        logs_dir=logs_dir, plan=plan, contracts_dir=contracts_dir
    )
    _write_json(
        logs_dir / "02_runtime" / "run_config.json",
        {
            "config_path": str(config_path),
            "task": task,
            "source_repo": str(source_repo),
            "plan_path": str(plan_path),
            "pareto_enabled": False,
            "slow_loop": config.get("slow_loop"),
            "fast_loop": config.get("fast_loop"),
            "codex_thread_policy": "fresh",
            "adaptation": "disabled_oneshot",
            "decomposition_mode": "dynamic_public_harness",
            "subtask_ids": [s.subtask_id for s in plan.subtasks],
            "keystone_harness_ids": [s.keystone_harness_id for s in plan.subtasks],
        },
    )
    _write_json(
        logs_dir / "00_decomposition" / "problem_artifact.json",
        problem.model_dump(mode="json"),
    )

    if dry_run:
        summary = {
            "mode": "realbench_dynamic_taskplan_public_harness",
            "dry_run": True,
            "task_id": task_id,
            "plan_task_id": plan.task_id,
            "subtasks": [s.subtask_id for s in plan.subtasks],
            "keystone_harness_ids": [s.keystone_harness_id for s in plan.subtasks],
            "decomposition_status": plan.decomposition_status.value,
            "decomposition_dir": str(deco_dir),
            "subgraphs_dir": str(graphs_dir),
            "source_repo": str(source_repo),
            "run_dir": str(run_dir),
            "slow_loop_enabled": False,
            "fast_loop_max_candidates": 0,
        }
        _write_json(run_dir / "summary.json", summary)
        _write_trace_md(run_dir=run_dir, plan=plan, state=None, summary=summary)
        return summary

    contracts = load_contracts(contracts_dir)
    contract_hash = hashlib.sha256(
        "".join(
            contracts[key].model_dump_json() for key in sorted(contracts)
        ).encode()
    ).hexdigest()
    llm = OpenAICompatibleAsyncClient()
    backend_registry, backend_manifest = resolve_backend_registry(
        mock_backends=False,
        client=llm,
        include_smolagents=False,
        include_codex=True,
    )
    if not backend_registry.has("codex_sdk"):
        raise RuntimeError(
            "codex_sdk unavailable; install with: uv sync --extra codex"
        )

    runtime_cfg = config.get("runtime") or {}
    runtime_cap = 1
    limits = RuntimeLimits(
        max_parallel_benchmark_tasks=1,
        max_parallel_nodes_per_task=int(
            runtime_cfg.get("max_parallel_nodes_per_task", 2)
        ),
        max_parallel_llm_calls=1,
        max_parallel_sandboxes=1,
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
        run_id=run_dir.name,
        graph_id=compiled.graph.graph_id,
    )

    # Hard-disable adaptation loops for this baseline.
    slow_loop_config = SlowLoopConfig(
        enabled=False,
        budget=SlowLoopBudget(max_updates_per_task=0, max_candidates_per_update=0),
        allowed_backend_assignments={"coding": ["codex_sdk"]},
        backend_model_pools={
            "codex_sdk": [os.getenv("CODEX_MODEL", "gpt-5.4")],
        },
    )
    fast_budget = FastLoopBudget(max_candidates=0, max_total_backend_calls=0)
    slow_loop = SlowLoopController(
        config=slow_loop_config,
        checkpoint_store=task_checkpoint_store,
    )
    scheduler = ReadySubtaskScheduler(
        runtime=runtime,
        artifact_store=artifact_store,
        task_checkpoint_store=task_checkpoint_store,
        contracts_dir=contracts_dir,
        source_repo=str(source_repo.resolve()),
        budget=fast_budget,
        slow_loop=slow_loop,
        slow_loop_config=slow_loop_config,
        max_concurrent_subtasks=runtime_cap,
        allow_concurrent_subtasks=False,
    )

    initial = create_artifact(
        problem, producer_node_id="__input__", task_id=plan.task_id
    )
    context = RunContext(
        run_id=f"{batch_dir.name}-{task_id}",
        task_id=plan.task_id,
        run_dir=run_dir,
        limits=limits,
        semaphores=RuntimeSemaphores(limits),
        contract_hash=contract_hash,
        allow_config_drift=allow_config_drift,
    )
    state = TaskExecutionState.from_plan(
        plan, artifact_store_ref=str(run_dir / "artifacts")
    )
    state.pareto_state = None

    await event_writer.append(
        TelemetryEvent(
            run_id=context.run_id,
            task_id=plan.task_id,
            graph_id=compiled.graph.graph_id,
            event_type="task_decomposition_completed",
            metadata={
                "decomposition_status": plan.decomposition_status.value,
                "subtask_ids": [s.subtask_id for s in plan.subtasks],
                "realbench_task_id": task_id,
                "slow_loop_enabled": False,
                "fast_loop_max_candidates": 0,
                "codex_thread_policy": "fresh",
            },
        )
    )

    error: str | None = None
    try:
        logger.info("starting ReadySubtaskScheduler.run_task (RealBench baseline)")
        state = await scheduler.run_task(
            plan,
            state,
            initial_artifacts=ArtifactBundle(slots={"problem": initial}),
            context=context,
            source_repo=str(source_repo.resolve()),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("realbench baseline run failed for %s", task_id)
        error = f"{type(exc).__name__}: {exc}"

    latency_ms = int((time.perf_counter() - wall_started) * 1000)
    _dump_post_run(logs_dir=logs_dir, state=state, run_dir=run_dir)
    summary = {
        "mode": "realbench_dynamic_taskplan_public_harness",
        "task_id": task_id,
        "plan_task_id": plan.task_id,
        "domain": task["domain"],
        "run_dir": str(run_dir),
        "source_repo": str(source_repo),
        "frozen": bool(state.frozen),
        "error": error,
        "latency_ms": latency_ms,
        "started_at": started_at.isoformat(),
        "pareto_enabled": False,
        "slow_loop_enabled": False,
        "fast_loop_max_candidates": 0,
        "codex_thread_policy": "fresh",
        "decomposition_status": plan.decomposition_status.value,
        "subtask_ids": [s.subtask_id for s in plan.subtasks],
        "backend_override": backend_manifest.get("backend_override"),
        "subtask_status": {
            sid: sub.status.value for sid, sub in state.subtasks.items()
        },
        "committed": [
            sid
            for sid, sub in state.subtasks.items()
            if sub.status.value == "committed"
        ],
        "m5_revision_count": len(state.plan_revision_history or []),
        "slow_loop_updates": getattr(state.slow_loop_state, "updates_applied", 0)
        if state.slow_loop_state
        else 0,
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
    _write_json(
        run_dir / "run_manifest.json",
        {
            "runner": "run_realbench_codex_decomp_baseline",
            "started_at": started_at.isoformat(),
            "config_path": str(config_path),
            "task": task,
            **backend_manifest,
            "slow_loop_enabled": False,
            "fast_loop_max_candidates": 0,
            "codex_thread_policy": "fresh",
        },
    )
    _write_trace_md(run_dir=run_dir, plan=plan, state=state, summary=summary)
    return summary


async def _run(args: argparse.Namespace) -> int:
    load_env_file(_repo_root() / ".env")
    resolve_runtime_settings(include_lcb_repository_default=False)
    # Host bwrap/userns workaround used by prior RealBench Codex runs.
    os.environ.setdefault("ADAMAS_CODEX_SANDBOX_OVERRIDE", "full_access")
    os.environ.setdefault("ADAMAS_ALLOW_UNTRUSTED_REPO_HARNESS", "1")

    config_path = Path(args.config)
    config = _load_yaml(config_path)
    if bool((config.get("pareto") or {}).get("enabled", False)):
        raise SystemExit("baseline requires pareto.enabled=false")
    if bool((config.get("slow_loop") or {}).get("enabled", False)):
        raise SystemExit("baseline requires slow_loop.enabled=false")

    selected = TASKS
    if args.task_id:
        wanted = set(args.task_id)
        selected = [t for t in TASKS if t["task_id"] in wanted]
        missing = wanted - {t["task_id"] for t in selected}
        if missing:
            raise SystemExit(f"unknown task_id(s): {sorted(missing)}")

    batch_id = args.run_id or f"rb-decomp-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    output_root = Path(
        args.output_root
        or (config.get("experiment") or {}).get(
            "output_root", "outputs/realbench_codex_decomp_baseline"
        )
    )
    batch_dir = (output_root / batch_id).resolve()
    batch_dir.mkdir(parents=True, exist_ok=True)
    _write_json(
        batch_dir / "batch_manifest.json",
        {
            "batch_id": batch_id,
            "tasks": selected,
            "config": str(config_path),
            "slow_loop_enabled": False,
            "fast_loop_max_candidates": 0,
            "codex_thread_policy": "fresh",
            "dry_run": bool(args.dry_run),
        },
    )

    results: list[dict[str, Any]] = []
    for task in selected:
        print(f"\n===== START {task['task_id']} =====", flush=True)
        summary = await _run_one(
            task=task,
            config=config,
            config_path=config_path,
            batch_dir=batch_dir,
            dry_run=bool(args.dry_run),
            allow_config_drift=bool(args.allow_config_drift),
        )
        results.append(summary)
        print(json.dumps(summary, indent=2, sort_keys=True, ensure_ascii=False))
        print(f"===== DONE {task['task_id']} =====\n", flush=True)

    batch_summary = {
        "batch_id": batch_id,
        "batch_dir": str(batch_dir),
        "n_tasks": len(results),
        "results": results,
        "committed_all": [
            r["task_id"]
            for r in results
            if set(r.get("committed") or []) >= {"analyze", "implement", "verify"}
        ],
        "errors": [
            {"task_id": r["task_id"], "error": r.get("error")}
            for r in results
            if r.get("error")
        ],
    }
    _write_json(batch_dir / "batch_summary.json", batch_summary)
    print(json.dumps(batch_summary, indent=2, sort_keys=True, ensure_ascii=False))
    print(f"batch_dir={batch_dir}")
    if batch_summary["errors"]:
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default="configs/experiments/realbench_codex_decomp_baseline.yaml",
    )
    parser.add_argument("--output-root")
    parser.add_argument("--run-id")
    parser.add_argument(
        "--task-id",
        action="append",
        help="Optional task filter; may be repeated. Default: all five baseline tasks.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only dump decomposition + subgraphs; do not call Codex.",
    )
    parser.add_argument("--allow-config-drift", action="store_true")
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
