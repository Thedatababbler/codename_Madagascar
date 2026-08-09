"""CodeProjectEval AdaMAS runner: risk-first milestones gated by real tests.

Plans a milestone DAG from the repository's PRD / architecture design / UML,
generates one runtime subgraph per milestone, and gates each milestone on the
dataset's own visible ``check_tests``. The held-out ``unit_tests`` suite never
enters the workspace and decides the score offline
(see ``scripts/eval_codeprojecteval.py``).

There is no template-segmentation fallback here: if the risk-first planner is
unavailable the run fails rather than silently measuring a shape-based split.
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

from orchestra.backends.factory import resolve_backend_registry
from orchestra.backends.health import healthcheck_used_backends
from orchestra.cli.run_m5_codex_demo import (
    _dump_decomposition,
    _dump_post_run,
    _dump_subgraphs,
    _write_trace_md,
)
from orchestra.cli.validate_graph import build_compiler
from orchestra.codeprojecteval import (
    build_agent_workspace,
    cpe_brief,
    load_task,
    materialize_check_harness,
)
from orchestra.codeprojecteval.ab import load_draft
from orchestra.codeprojecteval.dataset import DEFAULT_DATASET_ROOT, available_tasks
from orchestra.codeprojecteval.harness import (
    build_deterministic_contracts,
    check_command,
    contracts_path_for,
)
from orchestra.control.fast_loop.objectives import (
    DEFAULT_GATE_WEIGHT,
    DEFAULT_HARNESS_WEIGHT,
    DEFAULT_TOKEN_WEIGHT,
    TuningWeights,
    milestone_objectives,
)
from orchestra.control.fast_loop.schemas import FastLoopBudget
from orchestra.control.fast_loop.selector import DeterministicCandidateSelector
from orchestra.control.ready_scheduler import ReadySubtaskScheduler
from orchestra.control.slow_loop.controller import SlowLoopController
from orchestra.control.slow_loop.schemas import SlowLoopBudget, SlowLoopConfig
from orchestra.control.task_state import TaskExecutionState
from orchestra.decomposition.decomposer import TaskDecomposer
from orchestra.decomposition.realbench_plan import build_plan_from_draft
from orchestra.decomposition.schemas import DecompositionLimits, TaskPlan
from orchestra.executors.agent import AgentNodeExecutor
from orchestra.executors.harness import HarnessNodeExecutor
from orchestra.executors.registry import NodeExecutorRegistry
from orchestra.ir.artifacts import ArtifactBundle, create_artifact
from orchestra.ir.contracts import load_contracts
from orchestra.ir.graph import load_graph
from orchestra.llm.openai_compatible_async import OpenAICompatibleAsyncClient
from orchestra.realbench.milestone_planner import plan_milestones
from orchestra.realbench.subgraph_builder import (
    CODEPROJECTEVAL_PROMPT_PROFILE,
    generated_contracts_dir,
    prepare_generated_root,
)
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

SUPPORTED_AGENT_BACKENDS = frozenset({"codex_sdk", "smolagents_code"})
DEFAULT_HARNESS_TIMEOUT = 900
# Repositories whose reference implementation is green on both suites here.
USABLE_TASKS: tuple[str, ...] = (
    "bplustree",
    "csvs-to-sqlite",
    "deprecated",
    "imapclient",
    "parsel",
    "portalocker",
    "pyjwt",
    "python-hl7",
    "simpy",
    "tinydb",
    "voluptuous",
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False),
        encoding="utf-8",
    )


def _load_yaml(path: Path) -> dict[str, Any]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise SystemExit(f"config must be a mapping: {path}")
    return raw


def _selected_model_name(agent_backend: str) -> str:
    if agent_backend == "smolagents_code":
        return os.getenv("SMOLAGENTS_MODEL", "gpt-5-mini")
    return os.getenv("CODEX_MODEL", "gpt-5.4")


def _build_problem(task_id: str, prd: str) -> ProblemArtifact:
    return ProblemArtifact(
        question_id=task_id,
        title=task_id,
        statement=(
            f"CodeProjectEval task `{task_id}`.\n\n"
            "Implement the complete Python project described by docs/PRD.md, "
            "docs/architecture_design.md, the UML documents and "
            "docs/directory_tree.txt in the workspace. The repository ships a "
            "visible `check_tests/` suite that must pass; do not modify it.\n\n"
            f"{prd[:12000]}"
        ),
        difficulty="project",
        platform="codeprojecteval",
        starter_code="",
        public_examples=[],
    )


def build_cpe_task_plan(
    task_id: str,
    *,
    dataset_root: Path,
    workspace: Path,
    plan_path: Path,
    harness_dir: Path,
    experiment: dict[str, Any],
    agent_backend: str,
    contracts_dir: str = "configs/contracts",
    harness_timeout: int = DEFAULT_HARNESS_TIMEOUT,
    plan_file: Path | None = None,
) -> tuple[TaskPlan, str]:
    """Plan milestones and bind each to a real-test acceptance gate.

    ``plan_file`` replays a frozen planner draft, which an A/B needs: planner
    sampling variance would otherwise land inside the comparison.
    """
    task = load_task(task_id, dataset_root=dataset_root)
    deco_cfg = dict(experiment.get("decomposition") or {})
    generated_root = prepare_generated_root(
        plan_path.parent, base_contracts_dir=contracts_dir
    )
    effective_contracts_dir = str(generated_contracts_dir(generated_root))
    manifest = materialize_check_harness(task, harness_dir=harness_dir)
    env_python = Path(manifest.env_python)
    if not env_python.is_file():
        raise SystemExit(
            f"missing environment for {task_id}: {env_python}. Run "
            "scripts/probe_codeprojecteval_env.py first."
        )

    if plan_file is not None:
        draft = load_draft(Path(plan_file), max_milestones=int(deco_cfg.get("max_subtasks", 6)))
    else:
        draft = plan_milestones(
            task_id=task_id,
            workspace=task.repo_root,
            agent_backend=agent_backend,
            enable=True,
            max_milestones=int(deco_cfg.get("max_subtasks", 6)),
            brief=cpe_brief(task),
        )
    if draft is None:
        raise SystemExit(
            f"risk-first planner unavailable for {task_id}; refusing to fall back "
            "to template segmentation"
        )
    _write_json(plan_path.parent / "milestone_plan_draft.json", draft.to_dict())

    def bind(milestone: Any) -> list[str]:
        contracts = build_deterministic_contracts(
            task,
            role=milestone.role,
            focus_paths=list(milestone.focus_paths),
            milestone_id=milestone.milestone_id,
            extra_checks=list(milestone.acceptance.checks) or None,
            acceptance_criteria=list(milestone.acceptance.criteria) or None,
            corner_cases=list(milestone.acceptance.corner_cases) or None,
        )
        path = contracts_path_for(harness_dir, milestone.milestone_id)
        path.write_text(json.dumps(contracts, indent=2), encoding="utf-8")
        return check_command(
            harness_dir=harness_dir,
            level=milestone.role,
            env_python=env_python,
            contracts_path=path,
        )

    payload = build_plan_from_draft(
        task_id=task_id,
        draft=draft,
        workspace=workspace,
        generated_root=generated_root,
        harness_dir=harness_dir,
        agent_backend=agent_backend,
        model_name=_selected_model_name(agent_backend),
        harness_binder=bind,
        benchmark="codeprojecteval",
        prompt_profile=CODEPROJECTEVAL_PROMPT_PROFILE,
        harness_timeout_seconds=harness_timeout,
    )
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )

    decomposer = TaskDecomposer(
        enabled=True,
        default_graph_template=payload["subtasks"][-1]["local_graph_template"],
        keystone_harness_id="repository_test_harness",
        limits=DecompositionLimits(
            min_subtasks=int(deco_cfg.get("min_subtasks", 1)),
            max_subtasks=int(deco_cfg.get("max_subtasks", 6)),
            max_dependency_depth=int(deco_cfg.get("max_dependency_depth", 6)),
        ),
        require_graph_files=True,
        require_public_keystone_harness=bool(
            deco_cfg.get("require_public_keystone_harness", True)
        ),
    )
    plan = decomposer.decompose(
        task_id=payload["task_id"],
        objective=payload["subtasks"][0]["objective"],
        candidate_plan=payload,
        metadata={
            "plan_config": str(plan_path),
            "demo": "codeprojecteval_dynamic_taskplan",
            "codeprojecteval_task_id": task_id,
            "plan_builder": "build_plan_from_draft",
            "decomposition_source": draft.generator,
            "agent_backend": agent_backend,
        },
    )
    return plan, effective_contracts_dir


def read_tuning_config(config: dict[str, Any]) -> tuple[int, TuningWeights]:
    """How many repair candidates the fast loop may try, and what it optimises.

    Tuning happens *within* a milestone: once a milestone's gate has run, its
    result is known immediately, and the fast loop can retry that milestone
    alone rather than waiting for the whole repository to be scored. The three
    axes available at that point are the gate, the harness's graded score and
    the tokens spent -- deliberately not the held-out suite, which is not
    visible then and would be tuning on the test set if it were.
    """
    tuning = dict((config.get("experiment") or {}).get("tuning") or {})
    candidates = int(tuning.get("fast_loop_candidates", 0))
    if candidates < 0:
        raise ValueError("fast_loop_candidates cannot be negative")
    weights = TuningWeights(
        gate_weight=float(tuning.get("gate_weight", DEFAULT_GATE_WEIGHT)),
        harness_weight=float(tuning.get("harness_weight", DEFAULT_HARNESS_WEIGHT)),
        token_weight=float(tuning.get("token_weight", DEFAULT_TOKEN_WEIGHT)),
        token_reference=int(tuning.get("token_reference", 1_000_000)),
        allow_cost_to_outrank_gate=bool(tuning.get("allow_cost_to_outrank_gate", False)),
    )
    return candidates, weights


async def _run_one(
    *,
    task_id: str,
    config: dict[str, Any],
    config_path: Path,
    batch_dir: Path,
    dataset_root: Path,
    dry_run: bool,
    allow_config_drift: bool,
    agent_backend: str,
    plan_file: Path | None = None,
    arm: str = "planner",
) -> dict[str, Any]:
    started_at = datetime.now(UTC)
    wall_started = time.perf_counter()
    experiment = config["experiment"]
    contracts_dir = experiment.get("contracts_dir", "configs/contracts")
    harness_timeout = int(experiment.get("harness_timeout_seconds", DEFAULT_HARNESS_TIMEOUT))
    fast_loop_candidates, tuning_weights = read_tuning_config(config)

    run_dir = batch_dir / task_id
    logs_dir = run_dir / "logs"
    (logs_dir / "02_runtime").mkdir(parents=True, exist_ok=True)
    task = load_task(task_id, dataset_root=dataset_root)
    source_repo = build_agent_workspace(task, batch_dir / "workspaces" / task_id)
    harness_dir = run_dir / "harness"
    plan_path = run_dir / "plan.yaml"

    plan, contracts_dir = build_cpe_task_plan(
        task_id,
        dataset_root=dataset_root,
        workspace=source_repo,
        plan_path=plan_path,
        harness_dir=harness_dir,
        experiment=experiment,
        agent_backend=agent_backend,
        contracts_dir=contracts_dir,
        harness_timeout=harness_timeout,
        plan_file=plan_file,
    )
    problem = _build_problem(task_id, task.read(task.prd_path))
    selected_model = _selected_model_name(agent_backend)

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
    logger = logging.getLogger("codeprojecteval_decomp")
    logger.info(
        "task=%s backend=%s model=%s milestones=%s workspace=%s",
        task_id,
        agent_backend,
        selected_model,
        [s.subtask_id for s in plan.subtasks],
        source_repo,
    )

    deco_dir = _dump_decomposition(
        logs_dir=logs_dir, plan=plan, plan_path=str(plan_path)
    )
    graphs_dir = _dump_subgraphs(
        logs_dir=logs_dir, plan=plan, contracts_dir=contracts_dir
    )
    run_config = {
        "config_path": str(config_path),
        "task_id": task_id,
        "benchmark": "codeprojecteval",
        "source_repo": str(source_repo),
        "plan_path": str(plan_path),
        "harness_dir": str(harness_dir),
        "contracts_dir": contracts_dir,
        "agent_backend": agent_backend,
        "selected_model": selected_model,
        "decomposition_mode": "risk_first_dynamic",
        "arm": arm,
        "plan_file": str(plan_file) if plan_file else None,
        "decomposition_source": str(plan.metadata.get("decomposition_source") or ""),
        "acceptance_gate": "dataset_check_tests",
        "workspace_scaffold_policy": "dataset_only_prompt_delivered_contracts",
        "subtask_ids": [s.subtask_id for s in plan.subtasks],
        "milestone_agent_rosters": {
            s.subtask_id: s.metadata.get("agent_roster") or [] for s in plan.subtasks
        },
        "repository_editing": True,
        "workspace_isolation_policy": "shared_subtask_git_fork",
    }
    _write_json(logs_dir / "02_runtime" / "run_config.json", run_config)
    _write_json(
        logs_dir / "00_decomposition" / "problem_artifact.json",
        problem.model_dump(mode="json"),
    )

    if dry_run:
        summary = {
            "mode": "codeprojecteval_dynamic_taskplan",
            "dry_run": True,
            "task_id": task_id,
            "agent_backend": agent_backend,
            "selected_model": selected_model,
            "arm": arm,
            "subtasks": [s.subtask_id for s in plan.subtasks],
            "milestone_roles": {
                s.subtask_id: s.metadata.get("role") for s in plan.subtasks
            },
            "decomposition_status": plan.decomposition_status.value,
            "decomposition_dir": str(deco_dir),
            "subgraphs_dir": str(graphs_dir),
            "source_repo": str(source_repo),
            "run_dir": str(run_dir),
        }
        _write_json(run_dir / "summary.json", summary)
        _write_trace_md(run_dir=run_dir, plan=plan, state=None, summary=summary)
        return summary

    contracts = load_contracts(contracts_dir)
    contract_hash = hashlib.sha256(
        "".join(contracts[key].model_dump_json() for key in sorted(contracts)).encode()
    ).hexdigest()
    include_smolagents = agent_backend == "smolagents_code"
    include_codex = agent_backend == "codex_sdk"
    llm = OpenAICompatibleAsyncClient()
    backend_registry, backend_manifest = resolve_backend_registry(
        mock_backends=False,
        client=llm,
        include_smolagents=include_smolagents,
        include_codex=include_codex,
    )
    if not backend_registry.has(agent_backend):
        hint = (
            "uv sync --extra smolagents"
            if agent_backend == "smolagents_code"
            else "uv sync --extra codex"
        )
        raise RuntimeError(f"{agent_backend} unavailable; install with: {hint}")

    runtime_cfg = config.get("runtime") or {}
    limits = RuntimeLimits(
        max_parallel_benchmark_tasks=1,
        max_parallel_nodes_per_task=int(
            runtime_cfg.get("max_parallel_nodes_per_task", 2)
        ),
        max_parallel_llm_calls=1,
        max_parallel_sandboxes=1,
        max_concurrent_subtasks=1,
    )
    artifact_store = FileArtifactStore(run_dir)
    event_writer = AppendOnlyEventWriter(run_dir)
    runtime = NativeAsyncRuntime(
        executors=NodeExecutorRegistry(
            agent_executor=AgentNodeExecutor(contracts, backend_registry),
            harness_executor=HarnessNodeExecutor(
                MockSandbox(), timeout_seconds=harness_timeout
            ),
        ),
        artifact_store=artifact_store,
        checkpoint_store=CheckpointStore(run_dir),
        event_writer=event_writer,
    )

    compiled = build_compiler(contracts_dir).compile(
        load_graph(plan.subtasks[0].local_graph_template)
    )
    await healthcheck_used_backends(
        registry=backend_registry,
        graph=compiled,
        event_writer=event_writer,
        run_id=run_dir.name,
        graph_id=compiled.graph.graph_id,
    )

    slow_loop_config = SlowLoopConfig(
        enabled=False,
        budget=SlowLoopBudget(max_updates_per_task=0, max_candidates_per_update=0),
        allowed_backend_assignments={"coding": [agent_backend]},
        backend_model_pools={agent_backend: [selected_model]},
    )
    task_checkpoint_store = TaskCheckpointStore(run_dir)
    # Off by default: the A/B arms measure what decomposition buys, and a repair
    # loop that fires in one arm and not the other would be measured as part of
    # the arm. Turn it on deliberately, for tuning runs.
    fast_loop_budget = FastLoopBudget(
        max_candidates=fast_loop_candidates,
        max_total_backend_calls=fast_loop_candidates * 2,
    )
    scheduler = ReadySubtaskScheduler(
        runtime=runtime,
        artifact_store=artifact_store,
        task_checkpoint_store=task_checkpoint_store,
        contracts_dir=contracts_dir,
        source_repo=str(source_repo.resolve()),
        budget=fast_loop_budget,
        selector=DeterministicCandidateSelector(weights=tuning_weights),
        slow_loop=SlowLoopController(
            config=slow_loop_config, checkpoint_store=task_checkpoint_store
        ),
        slow_loop_config=slow_loop_config,
        max_concurrent_subtasks=1,
        allow_concurrent_subtasks=False,
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
                "codeprojecteval_task_id": task_id,
                "agent_backend": agent_backend,
                "selected_model": selected_model,
            },
        )
    )

    error: str | None = None
    try:
        state = await scheduler.run_task(
            plan,
            state,
            initial_artifacts=ArtifactBundle(
                slots={
                    "problem": create_artifact(
                        problem, producer_node_id="__input__", task_id=plan.task_id
                    )
                }
            ),
            context=context,
            source_repo=str(source_repo.resolve()),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("codeprojecteval run failed for %s", task_id)
        error = f"{type(exc).__name__}: {exc}"

    _dump_post_run(logs_dir=logs_dir, state=state, run_dir=run_dir)
    summary = {
        "mode": "codeprojecteval_dynamic_taskplan",
        "task_id": task_id,
        "benchmark": "codeprojecteval",
        "run_dir": str(run_dir),
        "source_repo": str(source_repo),
        "frozen": bool(state.frozen),
        "error": error,
        "latency_ms": int((time.perf_counter() - wall_started) * 1000),
        "started_at": started_at.isoformat(),
        "agent_backend": agent_backend,
        "selected_model": selected_model,
        "decomposition_status": plan.decomposition_status.value,
        "subtask_ids": [s.subtask_id for s in plan.subtasks],
        "milestone_count": len(plan.subtasks),
        "arm": arm,
        "plan_file": str(plan_file) if plan_file else None,
        "agent_turns": sum(
            len(s.metadata.get("agent_roster") or []) for s in plan.subtasks
        ),
        "subtask_status": {
            sid: sub.status.value for sid, sub in state.subtasks.items()
        },
        "committed": [
            sid for sid, sub in state.subtasks.items() if sub.status.value == "committed"
        ],
        "usage_records": len(state.backend_usage_records or []),
        "milestone_objectives": [o.to_dict() for o in milestone_objectives(state)],
        "tuning": {
            "fast_loop_candidates": fast_loop_candidates,
            "gate_weight": tuning_weights.gate_weight,
            "harness_weight": tuning_weights.harness_weight,
            "token_weight": tuning_weights.token_weight,
        },
        "backend_override": backend_manifest.get("backend_override"),
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
    return summary


async def _run(args: argparse.Namespace) -> int:
    load_env_file(_repo_root() / ".env")
    resolve_runtime_settings(include_lcb_repository_default=False)

    config_path = Path(args.config)
    config = _load_yaml(config_path)
    experiment = dict(config.get("experiment") or {})
    agent_backend = str(
        args.agent_backend or experiment.get("agent_backend") or "codex_sdk"
    )
    if agent_backend not in SUPPORTED_AGENT_BACKENDS:
        raise SystemExit(f"unsupported agent backend: {agent_backend}")
    if agent_backend == "codex_sdk":
        os.environ.setdefault("ADAMAS_CODEX_SANDBOX_OVERRIDE", "full_access")
    # The executed check script lives outside the workspace and is not
    # agent-writable, so the repository harness gate is opened explicitly.
    os.environ.setdefault("ADAMAS_ALLOW_UNTRUSTED_REPO_HARNESS", "1")

    dataset_root = Path(
        args.dataset_root or experiment.get("dataset_root") or DEFAULT_DATASET_ROOT
    )
    known = set(available_tasks(dataset_root))
    selected = list(args.task_id or USABLE_TASKS)
    unknown = [t for t in selected if t not in known]
    if unknown:
        raise SystemExit(f"unknown task_id(s): {unknown}")

    batch_id = args.run_id or f"cpe-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    output_root = Path(
        args.output_root
        or experiment.get("output_root", "outputs/codeprojecteval_decomp")
    )
    batch_dir = (output_root / batch_id).resolve()
    batch_dir.mkdir(parents=True, exist_ok=True)
    _write_json(
        batch_dir / "batch_manifest.json",
        {
            "batch_id": batch_id,
            "benchmark": "codeprojecteval",
            "tasks": selected,
            "config": str(config_path),
            "dataset_root": str(dataset_root),
            "agent_backend": agent_backend,
            "selected_model": _selected_model_name(agent_backend),
            "dry_run": bool(args.dry_run),
            "decomposition_mode": "risk_first_dynamic",
            "acceptance_gate": "dataset_check_tests",
        },
    )

    results: list[dict[str, Any]] = []
    for task_id in selected:
        print(f"\n===== START {task_id} ({agent_backend}) =====", flush=True)
        summary = await _run_one(
            task_id=task_id,
            config=config,
            config_path=config_path,
            batch_dir=batch_dir,
            dataset_root=dataset_root,
            dry_run=bool(args.dry_run),
            allow_config_drift=bool(args.allow_config_drift),
            agent_backend=agent_backend,
            plan_file=Path(args.plan_file) if args.plan_file else None,
            arm=str(args.arm),
        )
        results.append(summary)
        print(json.dumps(summary, indent=2, sort_keys=True, ensure_ascii=False))
        print(f"===== DONE {task_id} =====\n", flush=True)

    batch_summary = {
        "batch_id": batch_id,
        "batch_dir": str(batch_dir),
        "benchmark": "codeprojecteval",
        "agent_backend": agent_backend,
        "n_tasks": len(results),
        "results": results,
        "milestone_counts": {
            r["task_id"]: r.get("milestone_count") for r in results
        },
        "errors": [
            {"task_id": r["task_id"], "error": r.get("error")}
            for r in results
            if r.get("error")
        ],
    }
    _write_json(batch_dir / "batch_summary.json", batch_summary)
    print(json.dumps(batch_summary, indent=2, sort_keys=True, ensure_ascii=False))
    print(f"batch_dir={batch_dir}")
    return 1 if batch_summary["errors"] else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", default="configs/experiments/codeprojecteval_decomp.yaml"
    )
    parser.add_argument(
        "--agent-backend", choices=sorted(SUPPORTED_AGENT_BACKENDS), default=None
    )
    parser.add_argument("--dataset-root")
    parser.add_argument("--output-root")
    parser.add_argument("--run-id")
    parser.add_argument(
        "--task-id",
        action="append",
        help="Repository filter; may be repeated. Default: the environment-usable set.",
    )
    parser.add_argument(
        "--plan-file",
        help="Replay a frozen planner draft instead of sampling a new one.",
    )
    parser.add_argument(
        "--arm",
        default="planner",
        help="Label recorded in summaries, e.g. solo / single / multi.",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--allow-config-drift", action="store_true")
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
