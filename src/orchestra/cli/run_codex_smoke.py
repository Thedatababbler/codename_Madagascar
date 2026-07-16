"""Milestone 3.5 Codex smoke: TaskPlan → workspace → CodexSDK → repository harness."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from orchestra.backends.factory import build_default_backend_registry
from orchestra.backends.health import healthcheck_used_backends
from orchestra.cli.validate_graph import build_compiler
from orchestra.control.single_subtask import (
    SingleSubtaskCompatibilityRunner,
    summarize_task_state,
)
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


def _load_experiment(path: str) -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def _load_plan(path: str) -> TaskPlan:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return TaskPlan.model_validate(raw)


def _sdk_runtime_versions() -> dict[str, str | None]:
    sdk_version = None
    cli_version = None
    try:
        import importlib.metadata

        sdk_version = importlib.metadata.version("openai-codex")
    except Exception:  # noqa: BLE001
        try:
            import openai_codex

            sdk_version = getattr(openai_codex, "__version__", None)
        except ImportError:
            sdk_version = None
    try:
        import importlib.metadata

        cli_version = importlib.metadata.version("openai-codex-cli-bin")
    except Exception:  # noqa: BLE001
        cli_version = None
    return {
        "openai_codex": sdk_version,
        "openai_codex_cli_bin": cli_version,
    }


def _enrich_smoke_summary(
    *,
    state_summary: dict[str, Any],
    run_dir: Path,
    task_id: str,
    latency_ms: int,
    graph_frozen: bool | None,
) -> dict[str, Any]:
    """Add thread_id / patch hash / harness / usage / versions to smoke summary."""
    thread_id = None
    workspace_ref = None
    changed_files: list[str] = []
    patch = ""
    patch_hash = None
    harness_passed = None
    prompt_tokens = 0
    completion_tokens = 0

    subtasks = state_summary.get("subtasks") or {}
    for sub in subtasks.values():
        workspace_ref = workspace_ref or sub.get("workspace_ref")
        sessions = sub.get("backend_sessions") or []
        if isinstance(sessions, list):
            for record in sessions:
                if not isinstance(record, dict):
                    continue
                ref = record.get("session_ref") or {}
                thread_id = thread_id or ref.get("session_id")
        elif isinstance(sessions, dict):
            # Legacy summary shape (pre list[BackendSessionRecord]).
            codex_session = sessions.get("codex_sdk") or {}
            thread_id = thread_id or codex_session.get("session_id")

    # Prefer committed RepositoryChangeArtifact + harness result from disk.
    task_art_dir = run_dir / "tasks" / task_id / "artifacts"
    if task_art_dir.exists():
        for path in sorted(task_art_dir.glob("*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            art_type = raw.get("artifact_type")
            payload = raw.get("payload") or {}
            if art_type == "RepositoryChangeArtifact":
                thread_id = thread_id or payload.get("thread_id")
                workspace_ref = workspace_ref or payload.get("workspace_ref")
                changed_files = list(payload.get("changed_files") or changed_files)
                patch = str(payload.get("patch") or patch)
            elif art_type == "RepositoryHarnessResultArtifact":
                harness_passed = bool(payload.get("passed"))
                if payload.get("changed_files"):
                    changed_files = list(payload.get("changed_files") or [])

    if patch:
        patch_hash = hashlib.sha256(patch.encode("utf-8")).hexdigest()

    # Token usage from node telemetry events when present.
    events_path = run_dir / "events.jsonl"
    if events_path.exists():
        for line in events_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            prompt_tokens += int(event.get("prompt_tokens") or 0)
            completion_tokens += int(event.get("completion_tokens") or 0)
            usage = event.get("usage") or {}
            if isinstance(usage, dict):
                prompt_tokens += int(usage.get("prompt_tokens") or 0)
                completion_tokens += int(usage.get("completion_tokens") or 0)

    versions = _sdk_runtime_versions()
    summary = dict(state_summary)
    summary.update(
        {
            "graph_frozen": graph_frozen,
            "thread_id": thread_id,
            "workspace_ref": workspace_ref,
            "changed_files": changed_files,
            "patch_hash": patch_hash,
            "harness_passed": harness_passed,
            "token_usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
            },
            "latency_ms": latency_ms,
            "sdk_runtime_versions": versions,
            "task_id": task_id,
        }
    )
    return summary


async def _run(args: argparse.Namespace) -> int:
    load_env_file()
    started_at = datetime.now(UTC)
    wall_started = time.perf_counter()
    config = _load_experiment(args.config)
    experiment = config["experiment"]
    source_repo = args.source_repo or experiment["source_repo"]
    plan_path = args.plan or experiment["plan_config"]
    graph_path = experiment["graph_config"]
    contracts_dir = experiment.get("contracts_dir", "configs/contracts")
    output_root = Path(
        args.output_root or experiment.get("output_root", "outputs/m3_5_codex_smoke")
    )

    contracts = load_contracts(contracts_dir)
    contract_hash = hashlib.sha256(
        "".join(contracts[key].model_dump_json() for key in sorted(contracts)).encode()
    ).hexdigest()
    plan = _load_plan(plan_path)
    decomposer = TaskDecomposer(
        enabled=True,
        default_graph_template=graph_path,
        keystone_harness_id="repository_test_harness",
        require_graph_files=True,
    )
    plan = decomposer.decompose(
        task_id=plan.task_id,
        objective=plan.subtasks[0].objective,
        candidate_plan=plan,
        metadata={"plan_config": plan_path},
    )

    if args.dry_run:
        print(
            json.dumps(
                {
                    "mode": "m3_5_codex_smoke",
                    "task_id": plan.task_id,
                    "graph": graph_path,
                    "source_repo": source_repo,
                    "started_at": started_at.isoformat(),
                    "sdk_runtime_versions": _sdk_runtime_versions(),
                },
                indent=2,
            )
        )
        return 0

    run_id = args.run_id or f"m3_5_codex_smoke-{started_at.strftime('%Y%m%d%H%M%S')}"
    run_dir = output_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    # Codex path does not use MockAsyncLLMClient; keep a placeholder registry peer.
    llm = MockAsyncLLMClient({})
    backend_registry = build_default_backend_registry(
        llm, include_smolagents=False, include_codex=True
    )
    if not backend_registry.has("codex_sdk"):
        raise RuntimeError(
            "codex_sdk backend unavailable; install with: uv sync --extra codex"
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
    compiled = build_compiler(contracts_dir).compile(load_graph(graph_path))
    await healthcheck_used_backends(
        registry=backend_registry,
        graph=compiled,
        event_writer=event_writer,
        run_id=run_id,
        graph_id=compiled.graph.graph_id,
    )

    problem = ProblemArtifact(
        question_id=plan.task_id,
        title="Codex tiny repo fix",
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
    runner = SingleSubtaskCompatibilityRunner(
        runtime=runtime,
        artifact_store=artifact_store,
        task_checkpoint_store=task_checkpoint_store,
        contracts_dir=contracts_dir,
        source_repo=str(Path(source_repo).resolve()),
    )
    await event_writer.append(
        TelemetryEvent(
            run_id=run_id,
            task_id=plan.task_id,
            graph_id=compiled.graph.graph_id,
            event_type="task_decomposition_completed",
            metadata={
                "decomposition_status": plan.decomposition_status.value,
                "plan_version": plan.plan_version,
                "source_repo": source_repo,
            },
        )
    )
    try:
        state, result = await runner.run(
            plan=plan,
            initial_artifacts=ArtifactBundle(slots={"problem": initial}),
            context=context,
            source_repo=str(Path(source_repo).resolve()),
        )
    except Exception as exc:  # noqa: BLE001
        latency_ms = int((time.perf_counter() - wall_started) * 1000)
        summary = {
            "task_id": plan.task_id,
            "frozen": False,
            "error": f"{type(exc).__name__}: {exc}",
            "latency_ms": latency_ms,
            "sdk_runtime_versions": _sdk_runtime_versions(),
            "harness_passed": None,
            "thread_id": None,
            "changed_files": [],
            "patch_hash": None,
            "token_usage": {"prompt_tokens": 0, "completion_tokens": 0},
        }
        out = run_dir / "tasks" / plan.task_id / "codex_smoke_result.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(json.dumps(summary, indent=2))
        print(f"run_dir={run_dir}")
        return 1
    latency_ms = int((time.perf_counter() - wall_started) * 1000)
    base_summary = summarize_task_state(state)
    summary = _enrich_smoke_summary(
        state_summary=base_summary,
        run_dir=run_dir,
        task_id=plan.task_id,
        latency_ms=latency_ms,
        graph_frozen=None if result is None else result.state.frozen,
    )
    out = run_dir / "tasks" / plan.task_id / "codex_smoke_result.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    print(f"run_dir={run_dir}")
    ok = bool(state.frozen and summary.get("harness_passed") and summary.get("thread_id"))
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Milestone 3.5 Codex tiny-repo smoke")
    parser.add_argument("--config", default="configs/experiments/m3_5_codex_smoke.yaml")
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
