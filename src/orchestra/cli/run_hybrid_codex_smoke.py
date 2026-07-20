"""Deterministic Hybrid Codex Fast Loop smoke (fake AsyncCodex lifecycle)."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import yaml

from orchestra.backends.codex_sdk import CodexSDKBackend
from orchestra.backends.registry import AgentBackendRegistry
from orchestra.control.fast_loop.controller import FastLoopController
from orchestra.control.fast_loop.schemas import (
    CandidateStatus,
    CodexSessionMode,
    FastLoopBudget,
    FastLoopConfig,
    HybridCodexConfig,
    MissingParentPolicy,
)
from orchestra.control.single_subtask import SingleSubtaskCompatibilityRunner
from orchestra.control.task_state import SubtaskStatus
from orchestra.decomposition.schemas import TaskPlan
from orchestra.executors.agent import AgentNodeExecutor
from orchestra.executors.harness import HarnessNodeExecutor
from orchestra.executors.registry import NodeExecutorRegistry
from orchestra.ir.artifacts import ArtifactBundle, create_artifact
from orchestra.ir.contracts import load_contracts
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

CONTRACTS = "configs/contracts"
AGENT_NODE = "codex_implementer"


class _FakeTurn:
    final_response = "hybrid smoke ok"
    usage = SimpleNamespace(input_tokens=2, output_tokens=1)


def _write_calc(workspace: str, body: str) -> None:
    Path(workspace, "calculator.py").write_text(body, encoding="utf-8")


class _SmokeThread:
    def __init__(self, cwd: str, thread_id: str) -> None:
        self.cwd = cwd
        self.id = thread_id

    async def run(self, prompt: str, **kwargs):  # noqa: ANN003
        del kwargs
        if (
            "Previous attempt failed" in prompt
            or "Harness" in prompt
            or "pytest" in prompt
            or "alternative" in prompt.lower()
            or "Continue" in prompt
        ):
            _write_calc(self.cwd, "def add(a, b):\n    return a + b\n")
        else:
            _write_calc(self.cwd, "def add(a, b):\n    return a * b\n")
        return _FakeTurn()


class _SmokeCodex:
    """Fake AsyncCodex with thread_start/resume/fork for hybrid lifecycle demo."""

    def __init__(self) -> None:
        self._n = 0
        self.threads: dict[str, _SmokeThread] = {}
        self.calls: list[tuple[str, str | None]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):  # noqa: ANN001
        return None

    async def thread_start(self, **kwargs):  # noqa: ANN003
        self._n += 1
        tid = f"T{self._n - 1}"
        thread = _SmokeThread(kwargs["cwd"], tid)
        self.threads[tid] = thread
        self.calls.append(("start", None))
        return thread

    async def thread_resume(self, parent_thread_id: str, **kwargs):  # noqa: ANN003
        self.calls.append(("resume", parent_thread_id))
        parent = self.threads[parent_thread_id]
        return _SmokeThread(kwargs["cwd"], parent.id)

    async def thread_fork(self, parent_thread_id: str, **kwargs):  # noqa: ANN003
        self._n += 1
        tid = f"T{self._n - 1}"
        thread = _SmokeThread(kwargs["cwd"], tid)
        self.threads[tid] = thread
        self.calls.append(("fork", parent_thread_id))
        return thread


def _load_config(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _problem_bundle(task_id: str) -> ArtifactBundle:
    problem = ProblemArtifact(
        question_id=task_id,
        title="fix",
        statement="Fix calculator tests",
        difficulty="easy",
        platform="fixture",
    )
    art = create_artifact(problem, producer_node_id="__input__", task_id=task_id)
    return ArtifactBundle(slots={"problem": art})


def _runtime(tmp_path: Path, client: _SmokeCodex) -> NativeAsyncRuntime:
    contracts = load_contracts(CONTRACTS)
    registry = AgentBackendRegistry()
    registry.register(CodexSDKBackend(client_factory=lambda: client))
    return NativeAsyncRuntime(
        executors=NodeExecutorRegistry(
            agent_executor=AgentNodeExecutor(contracts, registry),
            harness_executor=HarnessNodeExecutor(MockSandbox()),
        ),
        artifact_store=FileArtifactStore(tmp_path),
        checkpoint_store=CheckpointStore(tmp_path),
        event_writer=AppendOnlyEventWriter(tmp_path),
    )


async def _run_smoke(config_path: Path, output: Path) -> dict:
    pytest_import = __import__("pytest")
    pytest_import.importorskip("openai_codex")

    cfg = _load_config(config_path)
    exp = cfg["experiment"]
    benchmark = cfg.get("benchmark", {})
    task_id = benchmark.get("task_id", "codex_tiny_repo")
    source_repo = exp.get("source_repo", "tests/fixtures/codex_tiny_repo")
    plan_path = exp["plan_config"]
    plan = TaskPlan.model_validate(yaml.safe_load(Path(plan_path).read_text(encoding="utf-8")))

    fast_loop_raw = cfg.get("fast_loop", {})
    hybrid_raw = fast_loop_raw.get("hybrid_codex", {})
    fl_config = FastLoopConfig(
        codex_session_mode=CodexSessionMode(
            fast_loop_raw.get("codex_session_mode", "hybrid")
        ),
        hybrid_codex=HybridCodexConfig(
            enable_resume=hybrid_raw.get("enable_resume", True),
            enable_fork=hybrid_raw.get("enable_fork", True),
            missing_parent_policy=MissingParentPolicy(
                hybrid_raw.get("missing_parent_policy", "reject")
            ),
            add_fresh_critic=hybrid_raw.get("add_fresh_critic", True),
        ),
        budget=FastLoopBudget(
            max_candidates=int(fast_loop_raw.get("max_candidates", 3)),
            max_total_backend_calls=int(fast_loop_raw.get("max_total_backend_calls", 10)),
        ),
    )

    output.mkdir(parents=True, exist_ok=True)
    client = _SmokeCodex()
    runtime = _runtime(output, client)
    limits = RuntimeLimits(
        max_parallel_benchmark_tasks=1,
        max_parallel_nodes_per_task=2,
        max_parallel_llm_calls=2,
        max_parallel_sandboxes=2,
    )
    ctx = RunContext(
        run_id="hybrid_codex_smoke",
        task_id=task_id,
        run_dir=output,
        limits=limits,
        semaphores=RuntimeSemaphores(limits),
        contract_hash="smoke",
    )
    ckpt = TaskCheckpointStore(output)

    runner = SingleSubtaskCompatibilityRunner(
        runtime=runtime,
        artifact_store=runtime.artifact_store,
        task_checkpoint_store=ckpt,
        contracts_dir=CONTRACTS,
        source_repo=source_repo,
    )
    state, _ = await runner.run(
        plan=plan,
        initial_artifacts=_problem_bundle(task_id),
        context=ctx,
        source_repo=source_repo,
    )
    initial_status = state.subtasks["implement_fix"].status
    parent_id = None
    if state.subtasks["implement_fix"].backend_sessions:
        parent_id = state.subtasks["implement_fix"].backend_sessions[0].session_ref.session_id

    controller = FastLoopController(
        runtime=runtime,
        artifact_store=runtime.artifact_store,
        task_checkpoint_store=ckpt,
        contracts_dir=CONTRACTS,
        fast_loop_config=fl_config,
    )
    state = await controller.run(
        state=state,
        subtask_id="implement_fix",
        context=ctx,
        initial_artifacts=_problem_bundle(task_id),
        source_repo=source_repo,
    )
    fl = state.fast_loop_states["implement_fix"]
    winner_id = fl.selected_candidate_id
    winner = next(c for c in fl.candidates if c.candidate_id == winner_id) if winner_id else None
    workspace_paths = {
        c.candidate_id: c.workspace_ref.path
        for c in fl.candidates
        if c.workspace_ref is not None
    }

    loaded = await ckpt.load(task_id)
    summary = {
        "config": str(config_path),
        "initial_status": initial_status.value,
        "final_status": state.subtasks["implement_fix"].status.value,
        "parent_thread_id": parent_id,
        "lifecycle_calls": client.calls,
        "candidate_ids": [c.candidate_id for c in fl.candidates],
        "candidate_statuses": {
            c.candidate_id: c.status.value for c in fl.candidates
        },
        "candidate_workspaces": workspace_paths,
        "selected_candidate_id": winner_id,
        "winner_committed": winner.status is CandidateStatus.COMMITTED if winner else False,
        "resume_directive_parent": (
            fl.candidates[0].session_directives.get(AGENT_NODE).source_session_ref.session_id
            if fl.candidates
            and fl.candidates[0].session_directives.get(AGENT_NODE)
            and fl.candidates[0].session_directives[AGENT_NODE].source_session_ref
            else None
        ),
        "lineage_record_count": len(loaded.session_lineage_records or []) if loaded else 0,
        "checkpoint_loaded": loaded is not None,
    }
    (output / "hybrid_codex_smoke_result.json").write_text(
        json.dumps(summary, indent=2),
        encoding="utf-8",
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    load_env_file()
    parser = argparse.ArgumentParser(description="Hybrid Codex deterministic smoke")
    parser.add_argument(
        "--config",
        default="configs/experiments/hybrid_codex_smoke.yaml",
    )
    parser.add_argument(
        "--output",
        default="outputs/hybrid_codex_smoke",
    )
    args = parser.parse_args(argv)
    summary = asyncio.run(_run_smoke(Path(args.config), Path(args.output)))
    print(json.dumps(summary, indent=2))
    ok = summary.get("final_status") == SubtaskStatus.COMMITTED.value
    ok = ok or summary.get("winner_committed") is True
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
