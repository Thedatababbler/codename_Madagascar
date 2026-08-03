"""Deterministic M6.1 runtime-correct Pareto smoke (no real API keys)."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import yaml

from orchestra.communication.payload import DeliveryRule, PayloadContract
from orchestra.communication.plan import CommunicationPlan
from orchestra.control.backend_usage import BackendUsageRecord
from orchestra.control.pareto.controller import ParetoGlobalCandidatePolicy
from orchestra.control.pareto.persistence import ParetoPersistence
from orchestra.control.pareto.schemas import (
    EvaluationVisibility,
    ObjectiveDirection,
    ParetoConfig,
    ParetoEvaluationKind,
    ParetoSearchState,
    PreferenceProfile,
    PublicEvaluationRecord,
)
from orchestra.control.slow_loop.controller import SlowLoopController
from orchestra.control.slow_loop.schemas import SlowLoopBudget, SlowLoopConfig
from orchestra.control.task_state import (
    SchedulerWaveRecord,
    SubtaskAttempt,
    SubtaskStatus,
    TaskExecutionState,
    WorkspaceCommitRecord,
    WorkspaceCommitStatus,
)
from orchestra.decomposition.schemas import BudgetSpec, SubtaskSpec, TaskPlan
from orchestra.runtime.backend import RunContext
from orchestra.runtime.limits import RuntimeLimits, RuntimeSemaphores
from orchestra.runtime.task_checkpoint import TaskCheckpointStore

GRAPH = "configs/graphs/codex_single_implementer.yaml"
OBJ = {
    "quality": ObjectiveDirection.MAXIMIZE,
    "cost": ObjectiveDirection.MINIMIZE,
    "latency": ObjectiveDirection.MINIMIZE,
    "risk": ObjectiveDirection.MINIMIZE,
}


def _load_config(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"M6 smoke config not found: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"M6 smoke config must be a mapping: {path}")
    return raw


def _pareto_config_from_yaml(cfg: dict) -> ParetoConfig:
    """Construct ParetoConfig via the shared typed control-plane loader."""
    from orchestra.control.pareto.runtime_factory import resolve_from_mapping

    # Smoke YAML may omit slow_loop.enabled; treat as enabled when pareto is on.
    mapping = dict(cfg)
    slow = dict(mapping.get("slow_loop") or {})
    pareto = dict(mapping.get("pareto") or {})
    if pareto.get("enabled", True) and "enabled" not in slow:
        slow["enabled"] = True
    if "enabled" not in pareto:
        pareto["enabled"] = True
    # Preserve smoke objective set when YAML omits objectives.
    if "objectives" not in pareto:
        pareto["objectives"] = {k: v.value for k, v in OBJ.items()}
    mapping["slow_loop"] = slow
    mapping["pareto"] = pareto
    return resolve_from_mapping(mapping).pareto_config


def _plan_from_config(cfg: dict) -> TaskPlan:
    task_id = str(cfg.get("task_id") or "m6_smoke")
    return TaskPlan(
        task_id=task_id,
        plan_version=1,
        decomposition_rationale="m6.1 runtime smoke",
        subtasks=[
            SubtaskSpec(
                subtask_id="s1",
                title="s1",
                objective="done",
                dependencies=[],
                keystone_harness_id="repository_test_harness",
                local_graph_template=GRAPH,
                budget=BudgetSpec(),
            ),
            SubtaskSpec(
                subtask_id="s2",
                title="s2",
                objective="future",
                dependencies=["s1"],
                keystone_harness_id="repository_test_harness",
                local_graph_template=GRAPH,
                budget=BudgetSpec(),
            ),
            SubtaskSpec(
                subtask_id="s3",
                title="s3",
                objective="future",
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


async def _run(output: Path, config_path: Path) -> dict:
    cfg = _load_config(config_path)
    output.mkdir(parents=True, exist_ok=True)
    plan = _plan_from_config(cfg)
    state = TaskExecutionState.from_plan(plan)
    state.communication_plan = plan.communication_plan
    state.subtasks["s1"].status = SubtaskStatus.COMMITTED
    state.subtasks["s2"].status = SubtaskStatus.PENDING
    state.subtasks["s3"].status = SubtaskStatus.PENDING
    state.committed_subtask_count = 1
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

    preference_id = str(
        ((cfg.get("pareto") or {}).get("preference_profile"))
        or cfg.get("preference_profile")
        or "balanced_knee"
    )
    pareto_config = _pareto_config_from_yaml(cfg)
    limits = RuntimeLimits(
        max_parallel_benchmark_tasks=1,
        max_parallel_nodes_per_task=2,
        max_parallel_llm_calls=2,
        max_parallel_sandboxes=2,
    )
    context = RunContext(
        run_id=plan.task_id,
        task_id=plan.task_id,
        run_dir=output,
        limits=limits,
        semaphores=RuntimeSemaphores(limits),
        contract_hash="c",
    )
    policy = ParetoGlobalCandidatePolicy(
        config=pareto_config,
        preference_profile=PreferenceProfile(profile_id=preference_id),
        run_dir=str(output),
    )
    ctrl = SlowLoopController(
        config=SlowLoopConfig(
            enabled=True,
            budget=SlowLoopBudget(
                context_pressure_ratio=0.5,
                min_commits_between_updates=1,
                max_candidates_per_update=max(1, int(pareto_config.max_candidates)),
            ),
            allowed_backend_assignments={"coding": ["codex_sdk", "smolagents_code"]},
        ),
        candidate_policy=policy,
        checkpoint_store=TaskCheckpointStore(output),
    )
    result = await ctrl.maybe_update(
        task_plan=plan,
        state=state,
        context=context,
        leased_subtask_ids=set(),
    )

    frontier_hashes: list[str] = []
    selected_hash = None
    if state.pareto_state and state.pareto_state.pending_decision:
        ctx_id = state.pareto_state.pending_decision.context.context_id
        frontier_hashes = sorted(
            c.content_hash
            for c in policy.archive.complete_frontier(ctx_id, ParetoEvaluationKind.ESTIMATED)
        )
        selected_hash = state.pareto_state.pending_decision.selected_content_hash
        assert selected_hash in frontier_hashes
        assert (
            state.pareto_state.pending_decision.activated_revision_id
            == state.active_plan_revision_id
        )

    # Decision-local realized finalization after a synthetic next wave.
    if state.pareto_state and state.pareto_state.pending_decision:
        started = state.pareto_state.pending_decision.started_at or now
        pending = state.pareto_state.pending_decision
        affected = list(
            pending.affected_subtask_ids
            or pending.context.eligible_future_subtask_ids
            or []
        )
        wave_id = "smoke-affected-wave"
        pending.affected_wave_id = wave_id
        pending.affected_subtask_ids = list(affected)
        usage_by_sid = {
            "s2": "wave-a",
            "s3": "wave-b",
        }
        state.backend_usage_records.extend(
            [
                BackendUsageRecord(
                    usage_id=usage_by_sid.get(sid, f"wave-{sid}"),
                    task_id=plan.task_id,
                    subtask_id=sid,
                    node_id=f"n-{sid}",
                    backend_id="codex_sdk",
                    attempt_id=1,
                    run_id=plan.task_id,
                    decision_id=pending.decision_id,
                    plan_revision=pending.activated_revision_id,
                    wave_id=wave_id,
                    scheduler_incarnation=1,
                    phase="post_activation",
                    started_at=started,
                    finished_at=datetime.fromtimestamp(started.timestamp() + 1.0, tz=UTC),
                    latency_seconds=1.0,
                    prompt_tokens=8,
                    completion_tokens=4,
                    estimated_cost_usd=0.01,
                    cost_quality="exact",
                    accounting_source="wave",
                    status="success",
                )
                for sid in affected
                if sid in state.subtasks
            ]
        )
        # Behavioral realization requires wave + attempt + commit + usage evidence.
        state.scheduler_wave_records = [
            SchedulerWaveRecord(
                wave_id=wave_id,
                run_id=plan.task_id,
                scheduler_incarnation=1,
                plan_revision=pending.activated_revision_id,
                policy_concurrency=2,
                runtime_cap=2,
                effective_concurrency=2,
                subtask_ids=list(affected),
                terminal_state="terminal",
                decision_id=pending.decision_id,
            )
        ]
        state.workspace_commit_records = []
        for sid in affected:
            if sid not in state.subtasks:
                continue
            uid = usage_by_sid.get(sid, f"wave-{sid}")
            lease_id = f"lease-{sid}"
            state.subtasks[sid].status = SubtaskStatus.COMMITTED
            state.subtasks[sid].lease_id = lease_id
            state.subtasks[sid].attempts = [
                SubtaskAttempt(
                    attempt_id=1,
                    status=SubtaskStatus.COMMITTED,
                    lease_id=lease_id,
                    wave_id=wave_id,
                    execution_plan_revision=pending.activated_revision_id,
                    scheduler_incarnation=1,
                    usage_ids=[uid],
                )
            ]
            state.workspace_commit_records.append(
                WorkspaceCommitRecord(
                    record_id=f"commit-{sid}",
                    task_id=plan.task_id,
                    subtask_id=sid,
                    attempt_id=1,
                    status=WorkspaceCommitStatus.COMMITTED,
                    run_id=plan.task_id,
                    lease_id=lease_id,
                    wave_id=wave_id,
                    execution_plan_revision=pending.activated_revision_id,
                    scheduler_incarnation=1,
                    decision_id=pending.decision_id,
                    usage_ids=[uid],
                    terminal_state="committed",
                    committed_at=now,
                )
            )
        # Restart recovery before finalize.
        store = TaskCheckpointStore(output)
        await store.save(state)
        loaded = await store.load(
            state.task_id,
            plan_version=state.task_plan.plan_version,
            plan_content_hash=state.task_plan.content_hash(),
            allow_config_drift=True,
        )
        assert loaded is not None
        assert loaded.pareto_state.pending_decision is not None
        assert loaded.pareto_state.pending_decision.selected_candidate_snapshot is not None
        restarted = ParetoGlobalCandidatePolicy(
            config=pareto_config,
            preference_profile=PreferenceProfile(profile_id=preference_id),
            run_dir=str(output),
        )
        restarted.finalize_realized(loaded)
        assert loaded.pareto_state.pending_decision is None
        assert loaded.pareto_state.decision_history
        realized = loaded.pareto_state.decision_history[-1].selected_candidate_snapshot
        assert realized is not None
        wall = realized.objectives.values["latency"].value
        assert wall is not None and wall < 2.0  # not sum of two 1s node latencies

    persistence = ParetoPersistence(output)
    summary = {
        "config_path": str(config_path),
        "config_loaded": True,
        "preference_profile": preference_id,
        "pareto_config": {
            "enabled": pareto_config.enabled,
            "max_candidates": pareto_config.max_candidates,
            "horizon_commits": pareto_config.horizon_commits,
            "fallback_to_rule_based": pareto_config.fallback_to_rule_based,
            "max_estimated_archive_size": pareto_config.max_estimated_archive_size,
            "max_realized_archive_size": pareto_config.max_realized_archive_size,
        },
        "slow_loop_message": result.message,
        "updated": result.updated,
        "complete_frontier": frontier_hashes,
        "selected_hash": selected_hash,
        "selected_in_frontier": bool(selected_hash and selected_hash in frontier_hashes),
        "checkpoint_restored": True,
        "estimated_archive_persisted": (output / "pareto" / "estimated_archive.json").exists(),
        "realized_archive_persisted": (output / "pareto" / "realized_archive.json").exists(),
        "search_traces_exist": (output / "pareto" / "search_traces.jsonl").exists(),
        "decisions_exist": bool(persistence.load_decisions()),
        "pareto_enabled": bool(pareto_config.enabled),
    }
    (output / "m6_smoke_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    assert summary["config_loaded"]
    assert summary["search_traces_exist"]
    if result.updated:
        assert summary["selected_in_frontier"]
        assert summary["estimated_archive_persisted"]
        assert summary["realized_archive_persisted"]
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default="configs/experiments/m6_pareto_smoke.yaml",
        help="experiment YAML loaded by the runtime smoke",
    )
    parser.add_argument(
        "--output",
        default="outputs/m6_pareto_smoke",
        type=Path,
    )
    args = parser.parse_args()
    summary = asyncio.run(_run(args.output, Path(args.config)))
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
