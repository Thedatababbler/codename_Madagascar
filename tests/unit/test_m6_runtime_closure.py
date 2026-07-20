"""M6.1 runtime-correct Pareto closure unit tests."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from orchestra.communication.ledger import DeliveryRecord, DeliveryStatus
from orchestra.communication.payload import DeliveryRule, PayloadContract
from orchestra.communication.plan import CommunicationPlan
from orchestra.control.backend_usage import BackendUsageRecord
from orchestra.control.pareto.archive import ParetoArchive
from orchestra.control.pareto.controller import ParetoGlobalCandidatePolicy
from orchestra.control.pareto.estimator import ParetoObjectiveEstimator
from orchestra.control.pareto.schemas import (
    EvaluationVisibility,
    ObjectiveDirection,
    ObjectiveSource,
    ObjectiveValue,
    ParetoConfig,
    ParetoEvaluationKind,
    ParetoObjectiveVector,
    ParetoOrchestraCandidate,
    PreferenceProfile,
    PublicEvaluationRecord,
)
from orchestra.control.pareto.selector import DeterministicParetoSelector
from orchestra.control.pareto.telemetry import realized_horizon_objectives
from orchestra.control.slow_loop.controller import SlowLoopController
from orchestra.control.slow_loop.schemas import (
    GlobalDiagnosis,
    GlobalDiagnosisReason,
    GlobalObservation,
    PendingBackendAssignmentEdit,
    SerializationGroupEdit,
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
OBJ4 = {
    "quality": ObjectiveDirection.MAXIMIZE,
    "cost": ObjectiveDirection.MINIMIZE,
    "latency": ObjectiveDirection.MINIMIZE,
    "risk": ObjectiveDirection.MINIMIZE,
}


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
                    payload_id="p23",
                    source_subtask_id="s2",
                    target_subtask_id="s3",
                    artifact_type="FinalAnswerArtifact",
                    required=False,
                    max_tokens=2048,
                    metadata={"slot": "comm:p23"},
                )
            ],
            delivery_schedule=[DeliveryRule(rule_id="r23", payload_id="p23", enabled=True)],
            context_budgets={"s3": 4000},
        ),
        metadata={
            "allowed_backend_assignments": {
                "coding": ["codex_sdk", "smolagents_code"],
                "s2": ["codex_sdk", "smolagents_code"],
                "s3": ["codex_sdk", "smolagents_code"],
            }
        },
    )


def _vec(**kwargs) -> ParetoObjectiveVector:
    return ParetoObjectiveVector(
        values={
            k: ObjectiveValue(
                value=float(v),
                source=ObjectiveSource.ESTIMATED,
                available=True,
                evaluation_visibility=EvaluationVisibility.PUBLIC,
            )
            for k, v in kwargs.items()
        },
        evaluation_kind=ParetoEvaluationKind.ESTIMATED,
    )


def _cand(cid: str, *, edits: int = 1, **kwargs) -> ParetoOrchestraCandidate:
    return ParetoOrchestraCandidate(
        candidate_id=cid,
        content_hash=cid,
        edit_signature=cid,
        context_id="ctx",
        edits=[{"type": "noop"} for _ in range(edits)],
        global_candidate=None,
        objectives=_vec(**kwargs),
        communication_overhead=float(edits),
    )


def _seed_public(state: TaskExecutionState, score: float = 1.0) -> None:
    state.public_evaluation_records = [
        PublicEvaluationRecord(
            evaluation_id="e1",
            task_id=state.task_id,
            harness_id="repository_test_harness",
            visibility=EvaluationVisibility.PUBLIC,
            passed=True,
            normalized_score=score,
            quality=score,
            created_at=datetime.now(UTC),
        )
    ]


def _usage(**kwargs) -> BackendUsageRecord:
    now = datetime.now(UTC)
    base = dict(
        usage_id="u",
        task_id="m61",
        subtask_id="s1",
        node_id="n",
        backend_id="codex_sdk",
        attempt_id=1,
        started_at=now,
        finished_at=now,
        latency_seconds=0.5,
        prompt_tokens=10,
        completion_tokens=5,
        estimated_cost_usd=0.01,
        cost_quality="exact",
        accounting_source="hist",
        status="success",
        model_name="fake-test-model",
    )
    base.update(kwargs)
    return BackendUsageRecord(**base)


def test_policy_selects_only_from_complete_frontier():
    cfg = ParetoConfig(objectives=OBJ4)
    archive = ParetoArchive(cfg)
    archive.insert(_cand("A", quality=1.0, cost=5.0, latency=2.0, risk=0.1))
    archive.insert(_cand("C", quality=0.5, cost=6.0, latency=3.0, risk=0.3))
    frontier = archive.complete_frontier("ctx")
    hashes = {c.content_hash for c in frontier}
    assert "A" in hashes
    assert "C" not in hashes


def test_dominated_candidate_never_reaches_selector():
    cfg = ParetoConfig(objectives=OBJ4)
    archive = ParetoArchive(cfg)
    strong = _cand("strong", edits=5, quality=1.0, cost=1.0, latency=1.0, risk=0.0)
    weak = _cand("weak", edits=1, quality=0.1, cost=2.0, latency=2.0, risk=1.0)
    archive.insert(strong)
    archive.insert(weak)
    frontier = archive.complete_frontier("ctx")
    assert all(c.content_hash != "weak" for c in frontier)
    chosen = DeterministicParetoSelector().select(
        frontier, PreferenceProfile(profile_id="balanced_knee"), OBJ4
    )
    assert chosen is not None
    assert chosen.content_hash != "weak"


def test_partial_candidate_excluded_from_balanced_profile():
    cfg = ParetoConfig(objectives=OBJ4)
    archive = ParetoArchive(cfg)
    archive.insert(_cand("p", quality=1.0, cost=1.0))
    assert archive.complete_frontier("ctx") == []
    assert archive.partial_candidates("ctx")
    chosen = DeterministicParetoSelector().select(
        archive.complete_frontier("ctx"),
        PreferenceProfile(profile_id="balanced_knee"),
        OBJ4,
    )
    assert chosen is None


def test_partial_candidate_excluded_from_quality_profile():
    cfg = ParetoConfig(objectives=OBJ4)
    archive = ParetoArchive(cfg)
    archive.insert(_cand("p", quality=1.0))
    chosen = DeterministicParetoSelector().select(
        archive.complete_frontier("ctx"),
        PreferenceProfile(profile_id="quality_first"),
        OBJ4,
    )
    assert chosen is None


def test_data_collection_can_explicitly_select_partial():
    cfg = ParetoConfig(objectives=OBJ4)
    archive = ParetoArchive(cfg)
    archive.insert(_cand("p", quality=0.8, cost=1.0))
    chosen = DeterministicParetoSelector().select(
        archive.partial_candidates("ctx"),
        PreferenceProfile(
            profile_id="data_collection",
            allow_partial_objectives=True,
            objective_weights={"quality": 1.0},
        ),
        {"quality": ObjectiveDirection.MAXIMIZE},
    )
    assert chosen is not None
    assert chosen.content_hash == "p"


def test_no_complete_frontier_returns_no_comparable_candidate():
    plan = _plan()
    state = TaskExecutionState.from_plan(plan)
    state.subtasks["s1"].status = SubtaskStatus.COMMITTED
    policy = ParetoGlobalCandidatePolicy(
        config=ParetoConfig(enabled=True, objectives=OBJ4),
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
        diagnosis=GlobalDiagnosis(update_required=True),
        eligible={"s2", "s3"},
        triggers=[],
    )
    proposal = policy.select(candidates, obs, state=state, leased_subtask_ids=set())
    assert proposal is not None
    assert proposal.selection_status.value == "no_comparable_candidate"
    assert proposal.selected_global_candidate is None


def test_rule_based_fallback_is_explicit():
    plan = _plan()
    state = TaskExecutionState.from_plan(plan)
    policy = ParetoGlobalCandidatePolicy(
        config=ParetoConfig(enabled=True, objectives=OBJ4, fallback_to_rule_based=True),
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
        diagnosis=GlobalDiagnosis(update_required=True),
        eligible={"s2", "s3"},
        triggers=[],
    )
    proposal = policy.select(candidates, obs, state=state, leased_subtask_ids=set())
    assert proposal.selection_status.value == "fallback_rule_based"


def test_selection_does_not_mutate_live_state():
    plan = _plan()
    state = TaskExecutionState.from_plan(plan)
    _seed_public(state)
    state.backend_usage_records = [_usage()]
    policy = ParetoGlobalCandidatePolicy(
        config=ParetoConfig(enabled=True, objectives=OBJ4),
        preference_profile=PreferenceProfile(profile_id="balanced_knee"),
    )
    obs = GlobalObservation(
        task_id="m61",
        state_version=0,
        active_plan_version=1,
        active_communication_version=1,
    )
    cands = policy.propose(
        task_plan=plan,
        communication_plan=plan.communication_plan,
        scheduling_policy=TaskSchedulingPolicy(max_concurrent_subtasks=2),
        state=state,
        observation=obs,
        diagnosis=GlobalDiagnosis(
            reasons=[GlobalDiagnosisReason.BUDGET_PRESSURE], update_required=True
        ),
        eligible={"s2", "s3"},
        triggers=[],
    )
    before = state.pareto_state
    proposal = policy.select(cands, obs, state=state, leased_subtask_ids=set())
    assert state.pareto_state is before
    assert proposal is not None
    assert proposal.projected_pareto_state is not None


def test_quality_unavailable_without_public_history():
    est = ParetoObjectiveEstimator()
    cand = _cand("x", cost=1.0, latency=1.0, risk=0.1)
    out = est.estimate(
        cand,
        observation=GlobalObservation(
            task_id="m61",
            state_version=0,
            active_plan_version=1,
            active_communication_version=1,
        ),
        public_evaluations=[],
    )
    assert out.objective_vector.values["quality"].available is False


def test_hidden_quality_record_rejected():
    est = ParetoObjectiveEstimator()
    cand = _cand("x", cost=1.0, latency=1.0, risk=0.1)
    out = est.estimate(
        cand,
        observation=GlobalObservation(
            task_id="m61",
            state_version=0,
            active_plan_version=1,
            active_communication_version=1,
        ),
        public_evaluations=[
            PublicEvaluationRecord(
                evaluation_id="h1",
                harness_id="hidden",
                visibility=EvaluationVisibility.HIDDEN,
                passed=True,
                normalized_score=1.0,
                quality=1.0,
            )
        ],
    )
    assert out.objective_vector.values["quality"].available is False


def test_quality_estimate_uses_only_public_development_history():
    est = ParetoObjectiveEstimator()
    cand = _cand("x")
    out = est.estimate(
        cand,
        observation=GlobalObservation(
            task_id="m61",
            state_version=0,
            active_plan_version=1,
            active_communication_version=1,
        ),
        public_evaluations=[
            PublicEvaluationRecord(
                evaluation_id="p1",
                visibility=EvaluationVisibility.PUBLIC,
                passed=True,
                normalized_score=0.8,
            ),
            PublicEvaluationRecord(
                evaluation_id="h1",
                visibility=EvaluationVisibility.PRIVATE,
                passed=True,
                normalized_score=0.1,
            ),
        ],
    )
    assert out.objective_vector.values["quality"].available is True
    assert out.objective_vector.values["quality"].value == pytest.approx(0.8)


def test_backend_assignment_changes_cost_estimate():
    est = ParetoObjectiveEstimator()
    history = [
        _usage(usage_id="a", backend_id="codex_sdk", estimated_cost_usd=0.10),
        _usage(usage_id="b", backend_id="smolagents_code", estimated_cost_usd=0.01),
    ]
    base = _cand("base")
    switched = _cand("sw")
    switched.edits = [
        PendingBackendAssignmentEdit(
            subtask_id="s2", node_id="agent", backend_id="smolagents_code"
        )
    ]
    obs = GlobalObservation(
        task_id="m61",
        state_version=0,
        active_plan_version=1,
        active_communication_version=1,
    )
    hist_state = type("S", (), {"backend_usage_records": history})()
    c0 = est.estimate(base, observation=obs, state=hist_state)
    c1 = est.estimate(switched, observation=obs, state=hist_state)
    assert c0.objective_vector.values["cost"].available
    assert c1.objective_vector.values["cost"].available
    assert c1.objective_vector.values["cost"].value != c0.objective_vector.values["cost"].value


def test_serialization_changes_wall_latency_estimate():
    est = ParetoObjectiveEstimator()
    history = [_usage(latency_seconds=1.0)]
    state = type("S", (), {"backend_usage_records": history})()
    obs = GlobalObservation(
        task_id="m61",
        state_version=0,
        active_plan_version=1,
        active_communication_version=1,
    )
    plain = _cand("p")
    serial = _cand("s")
    serial.edits = [SerializationGroupEdit(subtask_ids=["s2", "s3"])]
    a = est.estimate(plain, observation=obs, state=state)
    b = est.estimate(serial, observation=obs, state=state)
    assert b.objective_vector.values["latency"].value > a.objective_vector.values["latency"].value


def test_estimates_include_uncertainty_and_evidence_count():
    est = ParetoObjectiveEstimator()
    state = type("S", (), {"backend_usage_records": [_usage()]})()
    out = est.estimate(
        _cand("x"),
        observation=GlobalObservation(
            task_id="m61",
            state_version=0,
            active_plan_version=1,
            active_communication_version=1,
        ),
        state=state,
        public_evaluations=[
            PublicEvaluationRecord(
                evaluation_id="e",
                visibility=EvaluationVisibility.PUBLIC,
                passed=True,
                normalized_score=1.0,
            )
        ],
    )
    assert out.uncertainty
    assert out.evidence_counts
    assert out.estimator_version == "m6.1"


def test_backend_failures_are_usage_slice_local():
    plan = _plan()
    state = TaskExecutionState.from_plan(plan)
    now = datetime.now(UTC)
    state.backend_usage_records = [
        _usage(
            usage_id="old",
            status="model_failure",
            estimated_cost_usd=None,
            cost_quality="unavailable",
        ),
        _usage(usage_id="new", status="success", estimated_cost_usd=0.0),
    ]
    _, failures = realized_horizon_objectives(
        state,
        usage_start=1,
        delivery_start=0,
        commit_start=0,
        config=ParetoConfig(objectives=OBJ4),
        started_at=now,
        completed_at=now,
    )
    assert failures.backend_failures == 0


def test_old_backend_failure_not_counted():
    plan = _plan()
    state = TaskExecutionState.from_plan(plan)
    now = datetime.now(UTC)
    state.backend_usage_records = [
        _usage(
            usage_id="old",
            status="timeout",
            estimated_cost_usd=None,
            cost_quality="unavailable",
        )
    ]
    _, failures = realized_horizon_objectives(
        state,
        usage_start=1,
        delivery_start=0,
        commit_start=0,
        config=ParetoConfig(),
        started_at=now,
        completed_at=now,
    )
    assert failures.backend_failures == 0
    assert failures.timeouts == 0


def test_harness_failure_counted():
    plan = _plan()
    state = TaskExecutionState.from_plan(plan)
    now = datetime.now(UTC)
    state.backend_usage_records = [_usage(usage_id="h", status="harness_failure")]
    _, failures = realized_horizon_objectives(
        state,
        usage_start=0,
        delivery_start=0,
        commit_start=0,
        config=ParetoConfig(),
        started_at=now,
        completed_at=now,
    )
    assert failures.harness_failures >= 1


def test_timeout_counted():
    plan = _plan()
    state = TaskExecutionState.from_plan(plan)
    now = datetime.now(UTC)
    state.backend_usage_records = [_usage(usage_id="t", status="timeout")]
    _, failures = realized_horizon_objectives(
        state,
        usage_start=0,
        delivery_start=0,
        commit_start=0,
        config=ParetoConfig(),
        started_at=now,
        completed_at=now,
    )
    assert failures.timeouts == 1


def test_parallel_nodes_use_wall_latency_not_sum():
    plan = _plan()
    state = TaskExecutionState.from_plan(plan)
    now = datetime.now(UTC)
    completed = datetime.fromtimestamp(now.timestamp() + 1.0, tz=UTC)
    state.backend_usage_records = [
        _usage(usage_id=f"u{i}", latency_seconds=1.0, estimated_cost_usd=0.0) for i in range(2)
    ]
    vector, _ = realized_horizon_objectives(
        state,
        usage_start=0,
        delivery_start=0,
        commit_start=0,
        config=ParetoConfig(objectives=OBJ4),
        started_at=now,
        completed_at=completed,
    )
    assert vector.values["latency"].value == pytest.approx(1.0)


def test_partial_cost_is_unavailable():
    plan = _plan()
    state = TaskExecutionState.from_plan(plan)
    now = datetime.now(UTC)
    state.backend_usage_records = [
        _usage(usage_id="u1", estimated_cost_usd=0.01),
        _usage(
            usage_id="u2",
            estimated_cost_usd=None,
            cost_quality="unavailable",
            node_id="n2",
        ),
    ]
    vector, _ = realized_horizon_objectives(
        state,
        usage_start=0,
        delivery_start=0,
        commit_start=0,
        config=ParetoConfig(objectives=OBJ4),
        started_at=now,
        completed_at=now,
    )
    assert vector.values["cost"].available is False


def test_communication_uses_actual_tokens():
    plan = _plan()
    state = TaskExecutionState.from_plan(plan)
    now = datetime.now(UTC)
    state.delivery_ledger = [
        DeliveryRecord(
            delivery_id="d1",
            communication_plan_version=1,
            payload_id="p23",
            source_subtask_id="s2",
            target_subtask_id="s3",
            source_artifact_id="a",
            delivered_at_state_version=1,
            status=DeliveryStatus.DELIVERED,
            estimated_tokens=128,
            projected_token_count=128,
        )
    ]
    vector, _ = realized_horizon_objectives(
        state,
        usage_start=0,
        delivery_start=0,
        commit_start=0,
        config=ParetoConfig(
            objectives={**OBJ4, "communication_overhead": ObjectiveDirection.MINIMIZE}
        ),
        started_at=now,
        completed_at=now,
    )
    assert vector.values["communication_overhead"].value == pytest.approx(128.0)


def test_realized_quality_uses_public_harness():
    plan = _plan()
    state = TaskExecutionState.from_plan(plan)
    now = datetime.now(UTC)
    _seed_public(state, score=0.75)
    vector, _ = realized_horizon_objectives(
        state,
        usage_start=0,
        delivery_start=0,
        commit_start=0,
        config=ParetoConfig(objectives=OBJ4),
        started_at=now,
        completed_at=now,
        public_evaluation_start=0,
    )
    assert vector.values["quality"].available
    assert vector.values["quality"].value == pytest.approx(0.75)


@pytest.mark.asyncio
async def test_checkpoint_failure_leaves_no_pending_decision(tmp_path: Path):
    plan = _plan()
    state = TaskExecutionState.from_plan(plan)
    state.subtasks["s1"].status = SubtaskStatus.COMMITTED
    state.committed_subtask_count = 1
    state.communication_plan = plan.communication_plan.model_copy(
        update={"context_budgets": {"s3": 100}}
    )
    state.communication_plan.payload_contracts[0] = state.communication_plan.payload_contracts[
        0
    ].model_copy(update={"max_tokens": 9000})
    _seed_public(state)
    state.backend_usage_records = [_usage()]
    limits = RuntimeLimits(
        max_parallel_benchmark_tasks=1,
        max_parallel_nodes_per_task=1,
        max_parallel_llm_calls=1,
        max_parallel_sandboxes=1,
    )
    ctx = RunContext(
        run_id="m61",
        task_id="m61",
        run_dir=tmp_path,
        limits=limits,
        semaphores=RuntimeSemaphores(limits),
        contract_hash="c",
    )
    store = TaskCheckpointStore(tmp_path)
    store.save = AsyncMock(side_effect=RuntimeError("checkpoint boom"))
    policy = ParetoGlobalCandidatePolicy(
        config=ParetoConfig(enabled=True, objectives=OBJ4),
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
        task_plan=plan, state=state, context=ctx, leased_subtask_ids=set()
    )
    assert result.updated is False
    pending = getattr(state.pareto_state, "pending_decision", None)
    assert pending is None


def test_only_active_revision_can_finalize_decision():
    plan = _plan()
    state = TaskExecutionState.from_plan(plan)
    _seed_public(state)
    state.backend_usage_records = [_usage()]
    policy = ParetoGlobalCandidatePolicy(
        config=ParetoConfig(enabled=True, objectives=OBJ4),
        preference_profile=PreferenceProfile(profile_id="balanced_knee"),
    )
    obs = GlobalObservation(
        task_id="m61",
        state_version=0,
        active_plan_version=1,
        active_communication_version=1,
    )
    cands = policy.propose(
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
    proposal = policy.select(cands, obs, state=state, leased_subtask_ids=set())
    if proposal.selected_pareto_candidate is None:
        pytest.skip("no complete frontier in this fixture")
    state.pareto_state = proposal.projected_pareto_state
    state.pareto_state.pending_decision = state.pareto_state.pending_decision.model_copy(
        update={"activated_revision_id": "rev-other"}
    )
    state.active_plan_revision_id = "rev-active"
    policy.finalize_realized(state)
    assert state.pareto_state.pending_decision is not None


def test_search_trace_written_from_runtime_path(tmp_path: Path):
    plan = _plan()
    state = TaskExecutionState.from_plan(plan)
    _seed_public(state)
    state.backend_usage_records = [_usage()]
    policy = ParetoGlobalCandidatePolicy(
        config=ParetoConfig(enabled=True, objectives=OBJ4),
        preference_profile=PreferenceProfile(profile_id="balanced_knee"),
        run_dir=str(tmp_path),
    )
    obs = GlobalObservation(
        task_id="m61",
        state_version=0,
        active_plan_version=1,
        active_communication_version=1,
    )
    cands = policy.propose(
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
    policy.select(cands, obs, state=state, leased_subtask_ids=set())
    assert (tmp_path / "pareto" / "search_traces.jsonl").exists()


def test_missing_active_communication_hash_uses_content_hash():
    from orchestra.control.pareto.context import build_decision_context

    plan = _plan()
    state = TaskExecutionState.from_plan(plan)
    state.active_communication_hash = None
    ctx = build_decision_context(
        parent_plan_hash="plan",
        parent_communication_hash="",
        committed_prefix=[],
        eligible_future_subtask_ids=["s2"],
        triggers=[],
        diagnosis=GlobalDiagnosis(),
        preference_profile=PreferenceProfile(),
        backend_capabilities={},
        state=state,
        communication_plan=plan.communication_plan,
    )
    assert ctx.parent_communication_hash
    assert ctx.parent_communication_hash != ""
