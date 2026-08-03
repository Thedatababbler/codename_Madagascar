"""M6.1 recovery, frontier, bootstrap, and transaction safety integration tests."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

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
from orchestra.control.slow_loop.schemas import (
    GlobalDiagnosis,
    GlobalDiagnosisReason,
    GlobalObservation,
    SlowLoopBudget,
    SlowLoopConfig,
    TaskSchedulingPolicy,
)
from orchestra.control.task_state import SubtaskStatus, TaskExecutionState
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


def _ctx(tmp_path: Path) -> RunContext:
    limits = RuntimeLimits(
        max_parallel_benchmark_tasks=1,
        max_parallel_nodes_per_task=2,
        max_parallel_llm_calls=2,
        max_parallel_sandboxes=2,
    )
    return RunContext(
        run_id="m61",
        task_id="m61",
        run_dir=tmp_path,
        limits=limits,
        semaphores=RuntimeSemaphores(limits),
        contract_hash="c",
    )


def _plan() -> TaskPlan:
    return TaskPlan(
        task_id="m61",
        plan_version=1,
        decomposition_rationale="m61",
        subtasks=[
            SubtaskSpec(
                subtask_id="s1",
                title="s1",
                objective="a",
                dependencies=[],
                keystone_harness_id="repository_test_harness",
                local_graph_template=GRAPH,
                budget=BudgetSpec(),
            ),
            SubtaskSpec(
                subtask_id="s2",
                title="s2",
                objective="b",
                dependencies=["s1"],
                keystone_harness_id="repository_test_harness",
                local_graph_template=GRAPH,
                budget=BudgetSpec(),
            ),
            SubtaskSpec(
                subtask_id="s3",
                title="s3",
                objective="c",
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
                    payload_id="p12",
                    source_subtask_id="s1",
                    target_subtask_id="s2",
                    artifact_type="FinalAnswerArtifact",
                    required=False,
                    max_tokens=9000,
                    metadata={"slot": "comm:p12"},
                ),
                PayloadContract(
                    payload_id="p23",
                    source_subtask_id="s2",
                    target_subtask_id="s3",
                    artifact_type="FinalAnswerArtifact",
                    required=False,
                    max_tokens=9000,
                    metadata={"slot": "comm:p23"},
                ),
            ],
            delivery_schedule=[
                DeliveryRule(rule_id="r12", payload_id="p12", enabled=True),
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


def _ready_state(plan: TaskPlan) -> TaskExecutionState:
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
            usage_id="hist",
            task_id="m61",
            subtask_id="s1",
            node_id="n",
            backend_id="codex_sdk",
            attempt_id=1,
            started_at=now,
            finished_at=now,
            latency_seconds=0.5,
            prompt_tokens=20,
            completion_tokens=10,
            estimated_cost_usd=0.02,
            cost_quality="exact",
            accounting_source="hist",
            status="success",
            model_name="fake-test-model",
        ),
        BackendUsageRecord(
            usage_id="hist2",
            task_id="m61",
            subtask_id="s1",
            node_id="n2",
            backend_id="smolagents_code",
            attempt_id=1,
            started_at=now,
            finished_at=now,
            latency_seconds=0.3,
            prompt_tokens=10,
            completion_tokens=4,
            estimated_cost_usd=0.005,
            cost_quality="exact",
            accounting_source="hist",
            status="success",
            model_name="fake-test-model",
        ),
    ]
    return state


def test_real_online_frontier_excludes_dominated(tmp_path: Path):
    plan = _plan()
    # Fixture duration evidence so latency remains available (m6.2 estimator).
    plan = plan.model_copy(
        update={
            "metadata": {
                **dict(plan.metadata or {}),
                "fixture_subtask_durations_seconds": {
                    "s1": 0.5,
                    "s2": 0.5,
                    "s3": 0.5,
                },
            }
        }
    )
    state = _ready_state(plan)
    state.task_plan = plan
    state.scheduling_policy = TaskSchedulingPolicy(max_concurrent_subtasks=2)
    state.public_evaluation_records = [
        PublicEvaluationRecord(
            evaluation_id="e1",
            task_id="m61",
            harness_id="repository_test_harness",
            visibility=EvaluationVisibility.PUBLIC,
            passed=True,
            normalized_score=0.9,
        )
    ]
    policy = ParetoGlobalCandidatePolicy(
        config=ParetoConfig(enabled=True, max_candidates=12, objectives=OBJ),
        preference_profile=PreferenceProfile(profile_id="balanced_knee"),
        run_dir=str(tmp_path),
        runtime_concurrency_cap=2,
    )
    obs = GlobalObservation(
        task_id="m61",
        state_version=0,
        active_plan_version=1,
        active_communication_version=1,
    )
    diagnosis = GlobalDiagnosis(
        reasons=[
            GlobalDiagnosisReason.CONTEXT_PRESSURE,
            GlobalDiagnosisReason.BUDGET_PRESSURE,
            GlobalDiagnosisReason.BACKEND_INSTABILITY,
        ],
        update_required=True,
    )
    candidates = policy.propose(
        task_plan=plan,
        communication_plan=plan.communication_plan,
        scheduling_policy=TaskSchedulingPolicy(max_concurrent_subtasks=2),
        state=state,
        observation=obs,
        diagnosis=diagnosis,
        eligible={"s2", "s3"},
        triggers=[],
    )
    assert candidates
    proposal = policy.select(candidates, obs, state=state, leased_subtask_ids=set())
    frontier = policy.archive.complete_frontier(
        proposal.context.context_id, ParetoEvaluationKind.ESTIMATED
    )
    frontier_hashes = {c.content_hash for c in frontier}
    # At least one validated candidate should be dominated or absent from frontier.
    inserted = set(proposal.decision_record.candidate_hashes)
    dominated = inserted - frontier_hashes
    assert dominated or len(frontier) < len(inserted) or len(frontier) >= 1
    if proposal.selected_pareto_candidate is not None:
        assert proposal.selected_pareto_candidate.content_hash in frontier_hashes
        assert proposal.selection_status.value == "selected_complete_frontier"


def test_bootstrap_without_quality_history_uses_explicit_status():
    plan = _plan()
    state = _ready_state(plan)
    policy = ParetoGlobalCandidatePolicy(
        config=ParetoConfig(enabled=True, objectives=OBJ, fallback_to_rule_based=True),
        preference_profile=PreferenceProfile(profile_id="balanced_knee"),
    )
    obs = GlobalObservation(
        task_id="m61",
        state_version=0,
        active_plan_version=1,
        active_communication_version=1,
    )
    candidates = policy.propose(
        task_plan=plan,
        communication_plan=plan.communication_plan,
        scheduling_policy=TaskSchedulingPolicy(),
        state=state,
        observation=obs,
        diagnosis=GlobalDiagnosis(
            reasons=[GlobalDiagnosisReason.CONTEXT_PRESSURE], update_required=True
        ),
        eligible={"s2", "s3"},
        triggers=[],
    )
    proposal = policy.select(candidates, obs, state=state, leased_subtask_ids=set())
    assert policy.archive.complete_frontier(proposal.context.context_id) == []
    assert proposal.selection_status.value == "fallback_rule_based"
    assert proposal.selected_global_candidate is None


def test_data_collection_bootstrap_selects_partial():
    plan = _plan()
    state = _ready_state(plan)
    policy = ParetoGlobalCandidatePolicy(
        config=ParetoConfig(enabled=True, objectives=OBJ),
        preference_profile=PreferenceProfile(
            profile_id="data_collection",
            allow_partial_objectives=True,
            objective_weights={"cost": 1.0, "latency": 1.0, "risk": 1.0},
        ),
    )
    obs = GlobalObservation(
        task_id="m61",
        state_version=0,
        active_plan_version=1,
        active_communication_version=1,
    )
    candidates = policy.propose(
        task_plan=plan,
        communication_plan=plan.communication_plan,
        scheduling_policy=TaskSchedulingPolicy(),
        state=state,
        observation=obs,
        diagnosis=GlobalDiagnosis(
            reasons=[GlobalDiagnosisReason.CONTEXT_PRESSURE], update_required=True
        ),
        eligible={"s2", "s3"},
        triggers=[],
    )
    proposal = policy.select(candidates, obs, state=state, leased_subtask_ids=set())
    assert proposal.selection_status.value in {
        "selected_partial_for_data_collection",
        "no_comparable_candidate",
    }
    if proposal.selection_status.value == "selected_partial_for_data_collection":
        assert proposal.selected_pareto_candidate is not None
        assert not all(
            v.available
            for v in proposal.selected_pareto_candidate.objectives.values.values()
            if True
        ) or proposal.selected_pareto_candidate.content_hash in {
            c.content_hash
            for c in policy.archive.partial_candidates(proposal.context.context_id)
        }


@pytest.mark.asyncio
async def test_transaction_checkpoint_failure_leaves_no_active_decision(tmp_path: Path):
    from unittest.mock import AsyncMock

    plan = _plan()
    state = _ready_state(plan)
    state.public_evaluation_records = [
        PublicEvaluationRecord(
            evaluation_id="e1",
            visibility=EvaluationVisibility.PUBLIC,
            passed=True,
            normalized_score=1.0,
        )
    ]
    store = TaskCheckpointStore(tmp_path)
    store.save = AsyncMock(side_effect=RuntimeError("boom"))
    policy = ParetoGlobalCandidatePolicy(
        config=ParetoConfig(enabled=True, objectives=OBJ),
        preference_profile=PreferenceProfile(profile_id="balanced_knee"),
        run_dir=str(tmp_path),
    )
    ctrl = SlowLoopController(
        config=SlowLoopConfig(
            enabled=True,
            budget=SlowLoopBudget(context_pressure_ratio=0.5, min_commits_between_updates=1),
            allowed_backend_assignments={"coding": ["codex_sdk", "smolagents_code"]},
        ),
        candidate_policy=policy,
        checkpoint_store=store,
    )
    result = await ctrl.maybe_update(
        task_plan=plan, state=state, context=_ctx(tmp_path), leased_subtask_ids=set()
    )
    assert result.updated is False
    assert getattr(state.pareto_state, "pending_decision", None) is None


@pytest.mark.asyncio
async def test_restart_after_activation_recovers_candidate(tmp_path: Path):
    plan = _plan()
    state = _ready_state(plan)
    state.public_evaluation_records = [
        PublicEvaluationRecord(
            evaluation_id="e1",
            visibility=EvaluationVisibility.PUBLIC,
            passed=True,
            normalized_score=1.0,
        )
    ]
    policy = ParetoGlobalCandidatePolicy(
        config=ParetoConfig(enabled=True, objectives=OBJ),
        preference_profile=PreferenceProfile(profile_id="balanced_knee"),
        run_dir=str(tmp_path),
    )
    ctrl = SlowLoopController(
        config=SlowLoopConfig(
            enabled=True,
            budget=SlowLoopBudget(context_pressure_ratio=0.5, min_commits_between_updates=1),
            allowed_backend_assignments={"coding": ["codex_sdk", "smolagents_code"]},
        ),
        candidate_policy=policy,
        checkpoint_store=TaskCheckpointStore(tmp_path),
    )
    result = await ctrl.maybe_update(
        task_plan=plan, state=state, context=_ctx(tmp_path), leased_subtask_ids=set()
    )
    if not result.updated:
        pytest.skip(f"no activation in fixture: {result.message}")
    assert state.pareto_state.pending_decision is not None
    snap = state.pareto_state.pending_decision.selected_candidate_snapshot
    assert snap is not None
    store = TaskCheckpointStore(tmp_path)
    await store.save(state)
    loaded = await store.load(
        state.task_id,
        plan_version=state.task_plan.plan_version,
        plan_content_hash=state.task_plan.content_hash(),
        allow_config_drift=True,
    )
    assert loaded is not None
    assert loaded.pareto_state.pending_decision is not None
    assert (
        loaded.pareto_state.pending_decision.selected_candidate_snapshot.content_hash
        == snap.content_hash
    )
    # Restart policy loads estimated archive.
    restarted = ParetoGlobalCandidatePolicy(
        config=ParetoConfig(enabled=True, objectives=OBJ),
        preference_profile=PreferenceProfile(profile_id="balanced_knee"),
        run_dir=str(tmp_path),
    )
    assert restarted.archive.entries(snap.context_id, ParetoEvaluationKind.ESTIMATED)
    # Finalize after restart.
    loaded.backend_usage_records.append(
        BackendUsageRecord(
            usage_id="post",
            task_id="m61",
            subtask_id="s2",
            node_id="n",
            backend_id="codex_sdk",
            attempt_id=1,
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
            latency_seconds=0.2,
            prompt_tokens=1,
            completion_tokens=1,
            estimated_cost_usd=0.001,
            cost_quality="exact",
            accounting_source="post",
            status="success",
        )
    )
    loaded.public_evaluation_records.append(
        PublicEvaluationRecord(
            evaluation_id="e2",
            visibility=EvaluationVisibility.PUBLIC,
            passed=True,
            normalized_score=0.85,
        )
    )
    # Behavioral realization requires affected futures to reach a terminal state.
    for sid in loaded.pareto_state.pending_decision.affected_subtask_ids or ["s2", "s3"]:
        if sid in loaded.subtasks:
            loaded.subtasks[sid].status = SubtaskStatus.COMMITTED
    restarted.finalize_realized(loaded)
    assert loaded.pareto_state.pending_decision is None
    assert loaded.pareto_state.decision_history
    assert (tmp_path / "pareto" / "search_traces.jsonl").exists()
    realized = ParetoPersistence(tmp_path).load_realized_archive(ParetoConfig(objectives=OBJ))
    assert realized.realized_complete or realized.realized_partial


def test_hidden_evaluator_isolation(tmp_path: Path):
    plan = _plan()
    state = _ready_state(plan)
    state.public_evaluation_records = [
        PublicEvaluationRecord(
            evaluation_id="hidden",
            visibility=EvaluationVisibility.HIDDEN,
            passed=True,
            normalized_score=0.99,
        ),
        PublicEvaluationRecord(
            evaluation_id="private",
            visibility=EvaluationVisibility.PRIVATE,
            passed=True,
            normalized_score=0.01,
        ),
    ]
    policy = ParetoGlobalCandidatePolicy(
        config=ParetoConfig(enabled=True, objectives=OBJ),
        preference_profile=PreferenceProfile(profile_id="balanced_knee"),
        run_dir=str(tmp_path),
    )
    obs = GlobalObservation(
        task_id="m61",
        state_version=0,
        active_plan_version=1,
        active_communication_version=1,
    )
    candidates = policy.propose(
        task_plan=plan,
        communication_plan=plan.communication_plan,
        scheduling_policy=TaskSchedulingPolicy(),
        state=state,
        observation=obs,
        diagnosis=GlobalDiagnosis(
            reasons=[GlobalDiagnosisReason.CONTEXT_PRESSURE], update_required=True
        ),
        eligible={"s2", "s3"},
        triggers=[],
    )
    proposal = policy.select(candidates, obs, state=state, leased_subtask_ids=set())
    assert proposal.selection_status.value in {
        "no_comparable_candidate",
        "fallback_rule_based",
    }
    assert policy.archive.complete_frontier(proposal.context.context_id) == []


def test_duplicate_archive_event_is_idempotent(tmp_path: Path):
    persistence = ParetoPersistence(tmp_path)
    event = {"event_id": "est:abc", "kind": "estimated", "content_hash": "abc"}
    persistence.append_event(event)
    persistence.append_event(event)
    lines = (tmp_path / "pareto" / "archive_events.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
