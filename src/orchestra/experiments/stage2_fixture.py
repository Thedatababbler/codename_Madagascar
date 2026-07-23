"""Deterministic multi-subtask Stage-2 fixture (no paid API).

Drives ReadySubtaskScheduler + SlowLoopController + ParetoGlobalCandidatePolicy
with stubbed subtask execution so a real post-wave Pareto decision, M5
activation, future-subtask execution, and realized finalization occur.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import yaml

from orchestra.communication.payload import DeliveryRule, PayloadContract
from orchestra.communication.plan import CommunicationPlan
from orchestra.control.backend_usage import BackendUsageRecord
from orchestra.control.input_assembler import SubtaskInputAssembler
from orchestra.control.pareto.runtime_factory import (
    build_slow_loop_controller,
    control_plane_manifest_fields,
    resolve_from_mapping,
)
from orchestra.control.pareto.schemas import (
    EvaluationVisibility,
    ParetoSearchState,
    PublicEvaluationRecord,
)
from orchestra.control.ready_scheduler import (
    ReadySubtaskScheduler,
    SubtaskExecutionResult,
    SubtaskExecutionStatus,
)
from orchestra.control.task_state import SubtaskStatus, TaskExecutionState
from orchestra.decomposition.schemas import BudgetSpec, SubtaskSpec, TaskPlan
from orchestra.experiments.metadata import git_commit_hash
from orchestra.ir.artifacts import ArtifactBundle, create_artifact
from orchestra.runtime.backend import RunContext
from orchestra.runtime.limits import RuntimeLimits, RuntimeSemaphores
from orchestra.runtime.task_checkpoint import TaskCheckpointStore
from orchestra.schemas.artifacts import FinalAnswerArtifact
from orchestra.storage.artifacts import FileArtifactStore

GRAPH = "configs/graphs/codex_single_implementer.yaml"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def stage2_fixture_plan(task_id: str = "stage2_fixture") -> TaskPlan:
    return TaskPlan(
        task_id=task_id,
        plan_version=1,
        decomposition_rationale="stage2 multi-subtask fixture",
        subtasks=[
            SubtaskSpec(
                subtask_id="s1",
                title="s1",
                objective="seed",
                dependencies=[],
                keystone_harness_id="repository_test_harness",
                local_graph_template=GRAPH,
                budget=BudgetSpec(),
            ),
            SubtaskSpec(
                subtask_id="s2",
                title="s2",
                objective="adapt",
                dependencies=["s1"],
                keystone_harness_id="repository_test_harness",
                local_graph_template=GRAPH,
                budget=BudgetSpec(),
            ),
            SubtaskSpec(
                subtask_id="s3",
                title="s3",
                objective="finish",
                dependencies=["s2"],
                keystone_harness_id="repository_test_harness",
                local_graph_template=GRAPH,
                budget=BudgetSpec(),
            ),
        ],
        communication_plan=CommunicationPlan(
            version=1,
            payload_contracts=[
                PayloadContract(
                    payload_id="large",
                    source_subtask_id="s1",
                    target_subtask_id="s3",
                    artifact_type="FinalAnswerArtifact",
                    required=False,
                    max_tokens=9000,
                    metadata={"slot": "comm:large"},
                ),
                PayloadContract(
                    payload_id="p23",
                    source_subtask_id="s2",
                    target_subtask_id="s3",
                    artifact_type="FinalAnswerArtifact",
                    required=False,
                    max_tokens=4096,
                    metadata={"slot": "comm:p23"},
                ),
            ],
            delivery_schedule=[
                DeliveryRule(rule_id="r_large", payload_id="large", enabled=True),
                DeliveryRule(rule_id="r23", payload_id="p23", enabled=True),
            ],
            context_budgets={"s2": 1200, "s3": 1200},
        ),
        metadata={
            "allowed_backend_assignments": {
                "coding": ["codex_sdk", "smolagents_code"],
                "s2": ["codex_sdk", "smolagents_code"],
                "s3": ["codex_sdk", "smolagents_code"],
            }
        },
    )


def _ctx(run_dir: Path, task_id: str) -> RunContext:
    limits = RuntimeLimits(
        max_parallel_benchmark_tasks=1,
        max_parallel_nodes_per_task=2,
        max_parallel_llm_calls=2,
        max_parallel_sandboxes=2,
    )
    return RunContext(
        run_id=task_id,
        task_id=task_id,
        run_dir=run_dir,
        limits=limits,
        semaphores=RuntimeSemaphores(limits),
        contract_hash="stage2-fixture",
    )


async def run_stage2_fixture(
    config_path: str | Path,
    *,
    output_root: str | Path | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    repo_root = _repo_root()
    raw = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
    resolved = resolve_from_mapping(raw, repo_root=repo_root)
    if not resolved.slow_loop_config.enabled:
        raise RuntimeError("stage2 fixture requires slow_loop.enabled=true")

    started = datetime.now(UTC)
    mode = ((raw.get("stage2") or {}).get("mode")) or "fixture"
    experiment_name = ((raw.get("experiment") or {}).get("name")) or "stage2_fixture"
    root = Path(
        output_root
        or ((raw.get("stage2") or {}).get("output_root"))
        or ((raw.get("experiment") or {}).get("output_root"))
        or "outputs/stage2_pareto"
    )
    rid = run_id or f"fixture-{experiment_name}-{started.strftime('%Y%m%dT%H%M%SZ')}"
    run_dir = root / rid
    run_dir.mkdir(parents=True, exist_ok=True)

    plan = stage2_fixture_plan(task_id=f"stage2_{mode}")
    store = FileArtifactStore(run_dir)
    ckpt = TaskCheckpointStore(run_dir)
    state = TaskExecutionState.from_plan(plan, artifact_store_ref=str(run_dir / "artifacts"))
    state.communication_plan = plan.communication_plan
    # Seed first subtask as committed so Slow Loop can adapt future work.
    state.subtasks["s1"].status = SubtaskStatus.COMMITTED
    state.subtasks["s2"].status = SubtaskStatus.PENDING
    state.subtasks["s3"].status = SubtaskStatus.PENDING
    state.committed_subtask_count = 1
    if resolved.pareto_config.enabled:
        state.pareto_state = ParetoSearchState(enabled=True)
    now = datetime.now(UTC)
    state.backend_usage_records = [
        BackendUsageRecord(
            usage_id="hist-codex",
            task_id=plan.task_id,
            subtask_id="s1",
            node_id="n1",
            backend_id="codex_sdk",
            attempt_id=1,
            started_at=now,
            finished_at=now,
            latency_seconds=0.6,
            prompt_tokens=40,
            completion_tokens=12,
            estimated_cost_usd=0.03,
            cost_quality="exact",
            accounting_source="hist",
            status="success",
            model_name="fake-test-model",
        ),
        BackendUsageRecord(
            usage_id="hist-smol",
            task_id=plan.task_id,
            subtask_id="s1",
            node_id="n2",
            backend_id="smolagents_code",
            attempt_id=1,
            started_at=now,
            finished_at=now,
            latency_seconds=0.4,
            prompt_tokens=20,
            completion_tokens=8,
            estimated_cost_usd=0.008,
            cost_quality="exact",
            accounting_source="hist",
            status="success",
            model_name="fake-test-model",
        ),
    ]
    state.public_evaluation_records = [
        PublicEvaluationRecord(
            evaluation_id="pub1",
            task_id=plan.task_id,
            harness_id="repository_test_harness",
            visibility=EvaluationVisibility.PUBLIC,
            passed=True,
            normalized_score=0.92,
        )
    ]
    # Hidden/private records must never influence selection.
    state.public_evaluation_records.append(
        PublicEvaluationRecord(
            evaluation_id="hidden1",
            task_id=plan.task_id,
            harness_id="private_harness",
            visibility=EvaluationVisibility.HIDDEN,
            passed=True,
            normalized_score=0.01,
        )
    )

    slow_loop = build_slow_loop_controller(
        resolved,
        run_dir=run_dir,
        contracts_dir="configs/contracts",
        repo_root=repo_root,
        checkpoint_store=ckpt,
        fail_closed=True,
    )
    # Tighten trigger thresholds for deterministic fixture adaptation.
    slow_loop.config.budget.context_pressure_ratio = min(
        slow_loop.config.budget.context_pressure_ratio, 0.5
    )
    slow_loop.config.budget.min_commits_between_updates = 1

    runtime = AsyncMock()
    runtime.artifact_store = store
    sched = ReadySubtaskScheduler(
        runtime=runtime,
        artifact_store=store,
        task_checkpoint_store=ckpt,
        contracts_dir="configs/contracts",
        slow_loop=slow_loop,
        slow_loop_config=resolved.slow_loop_config,
        max_concurrent_subtasks=1,
    )
    sched.input_assembler = SubtaskInputAssembler(store)

    produced: dict[str, object] = {}

    async def _stub_run(**kwargs):  # noqa: ANN003
        sid = kwargs["subtask_id"]
        snapshot: TaskExecutionState = kwargs["state"]
        art = create_artifact(
            FinalAnswerArtifact(answer=f"out-{sid}", source_node=sid),
            producer_node_id=sid,
            task_id=plan.task_id,
        )
        await store.put(art)
        produced[sid] = art
        live = snapshot.subtasks[sid].model_copy(deep=True)
        live.status = SubtaskStatus.AWAITING_CANONICAL_COMMIT
        live.final_output_artifact_id = art.artifact_id
        live.communication_block_reason = None
        # Emit usage for realized horizon accounting.
        started_at = datetime.now(UTC)
        snapshot.backend_usage_records.append(
            BackendUsageRecord(
                usage_id=f"wave-{sid}",
                task_id=plan.task_id,
                subtask_id=sid,
                node_id=f"n-{sid}",
                backend_id="codex_sdk",
                attempt_id=1,
                started_at=started_at,
                finished_at=started_at,
                latency_seconds=0.5,
                prompt_tokens=8,
                completion_tokens=4,
                estimated_cost_usd=0.01,
                cost_quality="exact",
                accounting_source="wave",
                status="success",
            )
        )
        return SubtaskExecutionResult(
            subtask_id=sid,
            expected_state_version=kwargs.get("expected_state_version", 0),
            local_subtask_state=live,
            produced_artifacts=[art],
            execution_status=SubtaskExecutionStatus.SUCCESS_PENDING_COMMIT,
            candidate_harness_passed=True,
        )

    sched._run_subtask_isolated = _stub_run  # type: ignore[method-assign]

    manifest = {
        "runner": "stage2_fixture",
        "started_at": started.isoformat(),
        "git_commit": git_commit_hash(repo_root),
        "config_path": str(config_path),
        "stage2": raw.get("stage2") or {"mode": mode},
        "preference_profile_id": resolved.preference_profile.profile_id,
        **control_plane_manifest_fields(resolved),
    }
    (run_dir / "run_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    out = await sched.run_task(
        plan,
        state,
        initial_artifacts=ArtifactBundle(),
        context=_ctx(run_dir, plan.task_id),
    )

    # Finalize any pending realized decision after future commits.
    if (
        out.pareto_state is not None
        and out.pareto_state.pending_decision is not None
        and hasattr(slow_loop.candidate_policy, "finalize_realized")
    ):
        slow_loop.candidate_policy.finalize_realized(out)
        await ckpt.save(out)

    decisions = 0
    selected_hash = None
    pending = False
    if out.pareto_state is not None:
        decisions = len(out.pareto_state.decision_history)
        pending = out.pareto_state.pending_decision is not None
        if out.pareto_state.decision_history:
            selected_hash = out.pareto_state.decision_history[-1].selected_content_hash
        elif out.pareto_state.pending_decision is not None:
            selected_hash = out.pareto_state.pending_decision.selected_content_hash
            decisions = max(decisions, 1)

    summary = {
        "mode": mode,
        "run_dir": str(run_dir),
        "task_id": plan.task_id,
        "pareto_enabled": resolved.pareto_config.enabled,
        "preference_profile": resolved.preference_profile.profile_id,
        "committed": [
            sid for sid, sub in out.subtasks.items() if sub.status is SubtaskStatus.COMMITTED
        ],
        "active_plan_revision_id": out.active_plan_revision_id,
        "m5_revision_count": len(
            [r for r in out.plan_revision_history if str(r.status).endswith("APPLIED")
             or getattr(getattr(r, "status", None), "value", "") == "applied"]
        ),
        "decisions": decisions,
        "pending_decision": pending,
        "selected_hash": selected_hash,
        "execution_success_rate": (
            1.0
            if all(
                out.subtasks[s].status is SubtaskStatus.COMMITTED for s in ("s2", "s3")
            )
            else 0.0
        ),
        "difficulty": "fixture",
        "hidden_pass_at_1": "",  # evaluation-only; not claimed from fixture
        "restart_recovery_counts": 0,
        "control_plane_hash": resolved.control_plane_hash,
        "scheduler_path": "ReadySubtaskScheduler",
    }
    (run_dir / "stage2_fixture_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return summary
