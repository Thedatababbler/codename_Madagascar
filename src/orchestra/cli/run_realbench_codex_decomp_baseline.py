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
    build_plan_from_draft,
    build_realbench_candidate_plan,
    graph_catalog_for_backend,
)
from orchestra.decomposition.schemas import DecompositionLimits, TaskPlan
from orchestra.executors.agent import AgentNodeExecutor
from orchestra.executors.harness import HarnessNodeExecutor
from orchestra.executors.registry import NodeExecutorRegistry
from orchestra.ir.artifacts import ArtifactBundle, create_artifact
from orchestra.ir.contracts import load_contracts
from orchestra.ir.graph import load_graph
from orchestra.llm.openai_compatible_async import OpenAICompatibleAsyncClient
from orchestra.realbench.milestone_planner import plan_milestones
from orchestra.realbench.public_harness import materialize_public_harness
from orchestra.realbench.subgraph_builder import (
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
- Do not create subagents; implement directly in this workspace via the
  authorized repository-editing tools for the selected agent backend.
""",
        encoding="utf-8",
    )
    (ws / "README.md").write_text(
        f"# {task_id}\n\nSee TASK.md and public_design/.\n",
        encoding="utf-8",
    )
    # No AdaMAS assets here: contracts, harness and milestone briefs are
    # runner-owned and reach the agent through its prompt only.
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


def resolve_agent_backend(
    experiment: dict[str, Any],
    *,
    cli_backend: str | None = None,
) -> str:
    """Resolve selected agent backend; CLI overrides experiment config."""
    raw = cli_backend or experiment.get("agent_backend") or "codex_sdk"
    backend = str(raw).strip()
    if backend not in SUPPORTED_AGENT_BACKENDS:
        raise SystemExit(
            f"unsupported --agent-backend / experiment.agent_backend={backend!r}; "
            f"expected one of {sorted(SUPPORTED_AGENT_BACKENDS)}"
        )
    return backend


def resolve_graph_catalog(
    experiment: dict[str, Any],
    *,
    agent_backend: str,
) -> dict[str, str]:
    """Resolve graph catalog and fail closed on backend/graph family mismatch."""
    from orchestra.ir.nodes import AgentNodeSpec

    deco_cfg = dict(experiment.get("decomposition") or {})
    expected = graph_catalog_for_backend(agent_backend)
    catalog = dict(deco_cfg.get("graph_catalog") or expected)
    for role, path in expected.items():
        chosen = str(catalog.get(role) or path)
        catalog[role] = chosen
        if agent_backend == "smolagents_code" and "codex_realbench" in chosen:
            raise SystemExit(
                f"smolagents_code mode refuses Codex graph for {role}: {chosen}"
            )
        if agent_backend == "codex_sdk" and "smolagents_realbench" in chosen:
            raise SystemExit(
                f"codex_sdk mode refuses smolagents graph for {role}: {chosen}"
            )
    # Verify graph files declare the selected backend type.
    for role, path in catalog.items():
        graph = load_graph(path)
        agent_nodes = [n for n in graph.nodes if isinstance(n, AgentNodeSpec)]
        if not agent_nodes:
            raise SystemExit(f"graph family incomplete: no agent node in {path}")
        for node in agent_nodes:
            backend_type = node.resolved_backend().type
            if backend_type != agent_backend:
                raise SystemExit(
                    f"backend/graph mismatch: selected={agent_backend} but "
                    f"{path} node {node.node_id} uses {backend_type}"
                )
            if getattr(node.resolved_backend(), "require_git_diff", True) is False:
                raise SystemExit(
                    f"RealBench public graphs require require_git_diff=true ({path})"
                )
        if not str(graph.graph_id).startswith(("codex_realbench", "smolagents_realbench")):
            raise SystemExit(
                f"unexpected RealBench graph_id {graph.graph_id!r} in {path} ({role})"
            )
    return catalog


def _graph_catalog_hashes(catalog: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for role, path in sorted(catalog.items()):
        graph = load_graph(path)
        out[role] = graph.content_hash
    return out


def _tool_catalog_hash() -> str:
    from orchestra.ir.compiler import KNOWN_TOOL_IDS

    blob = "\n".join(sorted(KNOWN_TOOL_IDS)).encode()
    return hashlib.sha256(blob).hexdigest()


def build_dynamic_task_plan(
    task_id: str,
    *,
    workspace: Path,
    plan_path: Path,
    harness_dir: Path,
    experiment: dict[str, Any],
    agent_backend: str,
    contracts_dir: str = "configs/contracts",
) -> tuple[TaskPlan, str]:
    """Build + validate a milestone TaskPlan; return the plan and contracts dir.

    A risk-first planner draft wins when available (its milestones ship their own
    generated subgraphs and contracts); otherwise the deterministic
    public_design plan is used unchanged. Either way every milestone is bound to
    runner-owned harness assets under ``harness_dir``.
    """
    deco_cfg = dict(experiment.get("decomposition") or {})
    catalog = resolve_graph_catalog(experiment, agent_backend=agent_backend)
    force_split = deco_cfg.get("force_split")
    candidate_payload: dict[str, Any] | None = None
    generated_root = prepare_generated_root(
        plan_path.parent, base_contracts_dir=contracts_dir
    )
    effective_contracts_dir = str(generated_contracts_dir(generated_root))

    dynamic_cfg = deco_cfg.get("dynamic_planner")
    draft = plan_milestones(
        task_id=task_id,
        workspace=workspace,
        agent_backend=agent_backend,
        enable=None if dynamic_cfg is None else bool(dynamic_cfg),
        max_milestones=int(deco_cfg.get("max_subtasks", 6)),
    )
    if draft is not None:
        candidate_payload = build_plan_from_draft(
            task_id=task_id,
            draft=draft,
            workspace=workspace,
            generated_root=generated_root,
            harness_dir=harness_dir,
            agent_backend=agent_backend,
            model_name=_selected_model_name(agent_backend),
        )
        _write_json(
            plan_path.parent / "milestone_plan_draft.json", draft.to_dict()
        )

    if candidate_payload is None:
        # Template segmentation cuts by tree shape, not by risk: it is a
        # degraded fallback, never an experiment arm. Make the degradation
        # impossible to mistake for a planned run.
        logging.getLogger("realbench_decomp_baseline").error(
            "task=%s risk-first planner unavailable; falling back to TEMPLATE "
            "segmentation. Results from this task are not a decomposition "
            "measurement.",
            task_id,
        )
        (plan_path.parent / "PLANNER_FALLBACK").write_text(
            f"task={task_id}\nplan_source=template_fallback\n", encoding="utf-8"
        )
        candidate_payload = build_realbench_candidate_plan(
            task_id=task_id,
            workspace=workspace,
            generated_root=generated_root,
            harness_dir=harness_dir,
            graph_catalog=catalog,
            max_implementation_milestones=int(
                deco_cfg.get("max_implementation_milestones", 2)
            ),
            force_split=None if force_split is None else bool(force_split),
        )
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(
        yaml.safe_dump(candidate_payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    limits = DecompositionLimits(
        min_subtasks=int(deco_cfg.get("min_subtasks", 1)),
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
    plan = decomposer.decompose(
        task_id=candidate_payload["task_id"],
        objective=candidate_payload["subtasks"][0]["objective"],
        candidate_plan=candidate_payload,
        metadata={
            "plan_config": str(plan_path),
            "demo": "realbench_dynamic_taskplan_public_harness",
            "realbench_task_id": task_id,
            "plan_builder": str(
                (candidate_payload.get("metadata") or {}).get("plan_builder")
                or "build_realbench_candidate_plan"
            ),
            "decomposition_source": str(
                (candidate_payload.get("metadata") or {}).get("decomposition_source")
                or "public_design"
            ),
            "agent_backend": agent_backend,
        },
    )
    return plan, effective_contracts_dir


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


def _selected_model_name(agent_backend: str) -> str:
    if agent_backend == "smolagents_code":
        return os.getenv("SMOLAGENTS_MODEL", "gpt-5-mini")
    return os.getenv("CODEX_MODEL", "gpt-5.4")


def _smolagents_version() -> str | None:
    try:
        import smolagents

        return getattr(smolagents, "__version__", "installed")
    except ImportError:
        return None


def _public_harness_identity(manifest_path: Path) -> dict[str, Any]:
    if not manifest_path.is_file():
        return {"present": False}
    raw = manifest_path.read_bytes()
    return {
        "present": True,
        "path": str(manifest_path),
        "workspace_resident": False,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "payload": json.loads(raw.decode("utf-8")),
    }


async def _run_one(
    *,
    task: dict[str, str],
    config: dict[str, Any],
    config_path: Path,
    batch_dir: Path,
    dry_run: bool,
    allow_config_drift: bool,
    agent_backend: str,
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
    catalog = resolve_graph_catalog(experiment, agent_backend=agent_backend)
    graph_hashes = _graph_catalog_hashes(catalog)
    plan_path = run_dir / "plan.yaml"
    # Runner-owned harness assets; the agent workspace stays dataset-only.
    harness_dir = run_dir / "harness"
    public_manifest = materialize_public_harness(source_repo, harness_dir=harness_dir)
    os.environ["ADAMAS_PUBLIC_CHECK_SCRIPT"] = public_manifest.script_path
    os.environ["ADAMAS_PUBLIC_CHECK_MANIFEST"] = public_manifest.manifest_path
    plan, contracts_dir = build_dynamic_task_plan(
        task_id,
        workspace=source_repo,
        plan_path=plan_path,
        harness_dir=harness_dir,
        experiment=experiment,
        agent_backend=agent_backend,
        contracts_dir=contracts_dir,
    )
    requirements = (source_repo / "REQUIREMENTS.md").read_text(encoding="utf-8")
    problem = _build_problem(task, requirements)
    selected_model = _selected_model_name(agent_backend)
    harness_identity = _public_harness_identity(Path(public_manifest.manifest_path))

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
    logger = logging.getLogger("realbench_decomp_baseline")
    logger.info(
        "task=%s backend=%s model=%s run_dir=%s source_repo=%s subtasks=%s "
        "slow_loop=off fast_loop=off public_keystone=on",
        task_id,
        agent_backend,
        selected_model,
        run_dir,
        source_repo,
        [s.subtask_id for s in plan.subtasks],
    )

    deco_dir = _dump_decomposition(
        logs_dir=logs_dir, plan=plan, plan_path=str(plan_path)
    )
    _write_json(
        Path(deco_dir) / "public_harness_manifest.json", public_manifest.to_dict()
    )
    graphs_dir = _dump_subgraphs(
        logs_dir=logs_dir, plan=plan, contracts_dir=contracts_dir
    )
    run_config = {
        "config_path": str(config_path),
        "task": task,
        "source_repo": str(source_repo),
        "plan_path": str(plan_path),
        "pareto_enabled": False,
        "slow_loop": config.get("slow_loop"),
        "fast_loop": config.get("fast_loop"),
        "agent_backend": agent_backend,
        "selected_model": selected_model,
        "adaptation": "disabled_oneshot",
        "decomposition_mode": "dynamic_public_harness",
        "decomposition_source": str(
            plan.metadata.get("decomposition_source") or "public_design"
        ),
        "contracts_dir": contracts_dir,
        "harness_dir": str(harness_dir),
        "workspace_scaffold_policy": "dataset_only_prompt_delivered_contracts",
        "subtask_ids": [s.subtask_id for s in plan.subtasks],
        "keystone_harness_ids": [s.keystone_harness_id for s in plan.subtasks],
        "milestone_agent_rosters": {
            s.subtask_id: s.metadata.get("agent_roster") or []
            for s in plan.subtasks
        },
        "graph_catalog": catalog,
        "graph_hashes": graph_hashes,
        "repository_editing": True,
        "workspace_isolation_policy": "shared_subtask_git_fork",
    }
    if agent_backend == "codex_sdk":
        run_config["codex_thread_policy"] = "fresh"
    _write_json(logs_dir / "02_runtime" / "run_config.json", run_config)
    _write_json(
        logs_dir / "00_decomposition" / "problem_artifact.json",
        problem.model_dump(mode="json"),
    )

    if dry_run:
        summary = {
            "mode": "realbench_dynamic_taskplan_public_harness",
            "dry_run": True,
            "task_id": task_id,
            "agent_backend": agent_backend,
            "selected_model": selected_model,
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
            "graph_hashes": graph_hashes,
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
        install_hint = (
            "uv sync --extra smolagents"
            if agent_backend == "smolagents_code"
            else "uv sync --extra codex"
        )
        raise RuntimeError(f"{agent_backend} unavailable; install with: {install_hint}")
    # Fail closed: never silently register the opposite coding backend.
    if include_smolagents and backend_registry.has("codex_sdk"):
        raise RuntimeError(
            "smolagents RealBench baseline must not register codex_sdk"
        )
    if include_codex and backend_registry.has("smolagents_code"):
        raise RuntimeError(
            "codex RealBench baseline must not register smolagents_code"
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
    from orchestra.ir.nodes import AgentNodeSpec

    used = {
        n.resolved_backend().type
        for n in compiled.graph.nodes
        if isinstance(n, AgentNodeSpec)
    }
    if used != {agent_backend}:
        raise RuntimeError(
            f"health-check graph backends {sorted(used)} != selected {agent_backend}"
        )
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
        allowed_backend_assignments={"coding": [agent_backend]},
        backend_model_pools={agent_backend: [selected_model]},
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
                "agent_backend": agent_backend,
                "selected_model": selected_model,
            },
        )
    )

    error: str | None = None
    try:
        logger.info(
            "starting ReadySubtaskScheduler.run_task (RealBench baseline backend=%s)",
            agent_backend,
        )
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
        "agent_backend": agent_backend,
        "selected_model": selected_model,
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
    if agent_backend == "codex_sdk":
        summary["codex_thread_policy"] = "fresh"
    _write_json(run_dir / "summary.json", summary)
    manifest = {
        "runner": "run_realbench_codex_decomp_baseline",
        "started_at": started_at.isoformat(),
        "config_path": str(config_path),
        "task": task,
        **backend_manifest,
        "slow_loop_enabled": False,
        "fast_loop_max_candidates": 0,
        "agent_backend": agent_backend,
        "selected_backend_family": agent_backend,
        "selected_model": selected_model,
        "graph_catalog": catalog,
        "graph_ids": {
            role: load_graph(path).graph_id for role, path in catalog.items()
        },
        "graph_hashes": graph_hashes,
        "tool_catalog_hash": _tool_catalog_hash(),
        "repository_editing": True,
        "public_harness": harness_identity,
        "workspace_isolation_policy": "shared_subtask_git_fork",
        "seed": experiment.get("seed"),
        "budget": {
            "fast_loop_max_candidates": 0,
            "slow_loop_max_updates_per_task": 0,
            "max_concurrent_subtasks": runtime_cap,
        },
        "include_smolagents": include_smolagents,
        "include_codex": include_codex,
    }
    if agent_backend == "codex_sdk":
        manifest["codex_thread_policy"] = "fresh"
    if agent_backend == "smolagents_code":
        manifest["smolagents_version"] = _smolagents_version()
        if manifest["smolagents_version"] is None:
            raise RuntimeError("smolagents dependency missing after registry build")
    if manifest.get("selected_backend_family") != agent_backend:
        raise RuntimeError("manifest backend identity disagrees with selected backend")
    _write_json(run_dir / "run_manifest.json", manifest)
    _write_trace_md(run_dir=run_dir, plan=plan, state=state, summary=summary)
    return summary


async def _run(args: argparse.Namespace) -> int:
    load_env_file(_repo_root() / ".env")
    resolve_runtime_settings(include_lcb_repository_default=False)

    config_path = Path(args.config)
    config = _load_yaml(config_path)
    experiment = dict(config.get("experiment") or {})
    agent_backend = resolve_agent_backend(
        experiment, cli_backend=getattr(args, "agent_backend", None)
    )
    # Codex-only host workaround; never apply for smolagents mode.
    if agent_backend == "codex_sdk":
        os.environ.setdefault("ADAMAS_CODEX_SANDBOX_OVERRIDE", "full_access")
    # Workspaces are runner-generated from the dataset and no longer carry a
    # `.adamas_trusted_harness` marker (nothing AdaMAS-owned lives in them), so
    # the harness gate is opened explicitly here. The executed check script is
    # itself outside the workspace and therefore not agent-writable.
    os.environ.setdefault("ADAMAS_ALLOW_UNTRUSTED_REPO_HARNESS", "1")

    if bool((config.get("pareto") or {}).get("enabled", False)):
        raise SystemExit("baseline requires pareto.enabled=false")
    if bool((config.get("slow_loop") or {}).get("enabled", False)):
        raise SystemExit("baseline requires slow_loop.enabled=false")

    # Fail closed if experiment identity disagrees with CLI selection.
    exp_backend = experiment.get("agent_backend")
    if exp_backend and str(exp_backend) != agent_backend and args.agent_backend:
        # CLI wins, but record the override explicitly later in the batch manifest.
        pass
    if (
        agent_backend == "smolagents_code"
        and "codex" in str(experiment.get("name") or "").lower()
        and "smolagents" not in str(experiment.get("name") or "").lower()
        and not args.agent_backend
    ):
        raise SystemExit(
            "experiment name looks Codex-only but agent_backend=smolagents_code; "
            "use realbench_smolagents_decomp_baseline.yaml or pass --agent-backend"
        )

    selected = TASKS
    if args.task_id:
        wanted = set(args.task_id)
        selected = [t for t in TASKS if t["task_id"] in wanted]
        missing = wanted - {t["task_id"] for t in selected}
        if missing:
            raise SystemExit(f"unknown task_id(s): {sorted(missing)}")

    default_out = (
        "outputs/realbench_smolagents_decomp_baseline"
        if agent_backend == "smolagents_code"
        else "outputs/realbench_codex_decomp_baseline"
    )
    batch_id = args.run_id or f"rb-decomp-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    output_root = Path(
        args.output_root or experiment.get("output_root", default_out)
    )
    batch_dir = (output_root / batch_id).resolve()
    batch_dir.mkdir(parents=True, exist_ok=True)
    batch_manifest: dict[str, Any] = {
        "batch_id": batch_id,
        "tasks": selected,
        "config": str(config_path),
        "slow_loop_enabled": False,
        "fast_loop_max_candidates": 0,
        "agent_backend": agent_backend,
        "selected_backend_family": agent_backend,
        "selected_model": _selected_model_name(agent_backend),
        "dry_run": bool(args.dry_run),
        "graph_catalog": resolve_graph_catalog(
            experiment, agent_backend=agent_backend
        ),
        "graph_hashes": _graph_catalog_hashes(
            resolve_graph_catalog(experiment, agent_backend=agent_backend)
        ),
        "tool_catalog_hash": _tool_catalog_hash(),
        "repository_editing": True,
        "workspace_isolation_policy": "shared_subtask_git_fork",
        "seed": experiment.get("seed"),
    }
    if agent_backend == "codex_sdk":
        batch_manifest["codex_thread_policy"] = "fresh"
    if agent_backend == "smolagents_code":
        batch_manifest["smolagents_version"] = _smolagents_version()
        if batch_manifest["smolagents_version"] is None and not args.dry_run:
            raise SystemExit(
                "smolagents dependency missing; install with: uv sync --extra smolagents"
            )
    _write_json(batch_dir / "batch_manifest.json", batch_manifest)

    results: list[dict[str, Any]] = []
    for task in selected:
        print(f"\n===== START {task['task_id']} ({agent_backend}) =====", flush=True)
        summary = await _run_one(
            task=task,
            config=config,
            config_path=config_path,
            batch_dir=batch_dir,
            dry_run=bool(args.dry_run),
            allow_config_drift=bool(args.allow_config_drift),
            agent_backend=agent_backend,
        )
        results.append(summary)
        print(json.dumps(summary, indent=2, sort_keys=True, ensure_ascii=False))
        print(f"===== DONE {task['task_id']} =====\n", flush=True)

    batch_summary = {
        "batch_id": batch_id,
        "batch_dir": str(batch_dir),
        "agent_backend": agent_backend,
        "n_tasks": len(results),
        "results": results,
        "committed_all": [
            r["task_id"]
            for r in results
            if r.get("committed") and not r.get("error")
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
    parser.add_argument(
        "--agent-backend",
        choices=sorted(SUPPORTED_AGENT_BACKENDS),
        default=None,
        help="Agent backend family (overrides experiment.agent_backend).",
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
        help="Only dump decomposition + subgraphs; do not call model backends.",
    )
    parser.add_argument("--allow-config-drift", action="store_true")
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
