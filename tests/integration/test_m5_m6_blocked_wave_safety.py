"""M5/M6: required communication blocks are hard feasibility, not Pareto objectives."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from orchestra.communication.ledger import DeliveryFailureReason
from orchestra.communication.payload import DeliveryRule, PayloadContract
from orchestra.communication.plan import CommunicationPlan
from orchestra.control.backend_usage import BackendUsageRecord
from orchestra.control.input_assembler import SubtaskInputAssembler
from orchestra.control.pareto.candidate import build_pareto_candidate
from orchestra.control.pareto.context import build_decision_context
from orchestra.control.pareto.controller import ParetoGlobalCandidatePolicy
from orchestra.control.pareto.schemas import (
    EvaluationVisibility,
    ObjectiveDirection,
    ParetoConfig,
    PreferenceProfile,
    PublicEvaluationRecord,
)
from orchestra.control.pareto.validation import ParetoCandidateValidator
from orchestra.control.ready_scheduler import (
    ReadySubtaskScheduler,
    SubtaskExecutionResult,
    SubtaskExecutionStatus,
)
from orchestra.control.slow_loop.candidate_generator import RuleBasedGlobalCandidateGenerator
from orchestra.control.slow_loop.communication_safety import (
    active_required_communication_blocks,
    candidate_resolves_active_required_blocks,
)
from orchestra.control.slow_loop.controller import SlowLoopController
from orchestra.control.slow_loop.edits import apply_global_edits
from orchestra.control.slow_loop.schemas import (
    GlobalCandidate,
    GlobalCandidateValidationStatus,
    GlobalDiagnosis,
    GlobalDiagnosisReason,
    GlobalObservation,
    GlobalPlanRevisionStatus,
    SchedulingConcurrencyEdit,
    SlowLoopBudget,
    SlowLoopConfig,
    TaskSchedulingPolicy,
    UpsertDeliveryRuleEdit,
)
from orchestra.control.task_state import SubtaskStatus, TaskExecutionState
from orchestra.decomposition.schemas import BudgetSpec, SubtaskSpec, TaskPlan
from orchestra.ir.artifacts import ArtifactBundle, create_artifact
from orchestra.runtime.backend import RunContext
from orchestra.runtime.limits import RuntimeLimits, RuntimeSemaphores
from orchestra.runtime.task_checkpoint import TaskCheckpointStore
from orchestra.schemas.artifacts import FinalAnswerArtifact
from orchestra.storage.artifacts import FileArtifactStore

GRAPH = "configs/graphs/codex_single_implementer.yaml"
OBJ = {
    "quality": ObjectiveDirection.MAXIMIZE,
    "cost": ObjectiveDirection.MINIMIZE,
    "latency": ObjectiveDirection.MINIMIZE,
    "risk": ObjectiveDirection.MINIMIZE,
}


def _ctx(tmp_path: Path, task_id: str = "m5m6") -> RunContext:
    limits = RuntimeLimits(
        max_parallel_benchmark_tasks=1,
        max_parallel_nodes_per_task=1,
        max_parallel_llm_calls=1,
        max_parallel_sandboxes=1,
    )
    return RunContext(
        run_id=task_id,
        task_id=task_id,
        run_dir=tmp_path,
        limits=limits,
        semaphores=RuntimeSemaphores(limits),
        contract_hash="c",
    )


def _missing_rule_plan(task_id: str = "m5m6") -> TaskPlan:
    return TaskPlan(
        task_id=task_id,
        plan_version=1,
        decomposition_rationale="m5/m6 blocked-wave safety",
        subtasks=[
            SubtaskSpec(
                subtask_id="s1",
                title="s1",
                objective="src",
                dependencies=[],
                keystone_harness_id="repository_test_harness",
                local_graph_template=GRAPH,
                budget=BudgetSpec(),
            ),
            SubtaskSpec(
                subtask_id="s2",
                title="s2",
                objective="blocked",
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
                    payload_id="p1",
                    source_subtask_id="s1",
                    target_subtask_id="s2",
                    artifact_type="FinalAnswerArtifact",
                    required=True,
                )
            ],
            delivery_schedule=[],
            context_budgets={"s2": 2048, "s3": 2048},
        ),
        metadata={
            "allowed_backend_assignments": {
                "coding": ["codex_sdk", "smolagents_code"],
                "s2": ["codex_sdk", "smolagents_code"],
                "s3": ["codex_sdk", "smolagents_code"],
            }
        },
    )


async def _seed(
    tmp_path: Path, *, task_id: str = "m5m6"
) -> tuple[TaskPlan, TaskExecutionState, FileArtifactStore]:
    store = FileArtifactStore(tmp_path / "artifacts")
    plan = _missing_rule_plan(task_id)
    state = TaskExecutionState.from_plan(plan)
    state.communication_plan = plan.communication_plan
    state.scheduling_policy = TaskSchedulingPolicy(max_concurrent_subtasks=2)
    art = create_artifact(
        FinalAnswerArtifact(answer="upstream", source_node="s1"),
        producer_node_id="s1",
        task_id=task_id,
    )
    await store.put(art)
    state.subtasks["s1"].status = SubtaskStatus.COMMITTED
    state.subtasks["s1"].final_output_artifact_id = art.artifact_id
    state.subtasks["s2"].status = SubtaskStatus.READY
    state.subtasks["s3"].status = SubtaskStatus.PENDING
    state.committed_subtask_count = 1
    now = datetime.now(UTC)
    state.backend_usage_records = [
        BackendUsageRecord(
            usage_id="hist-a",
            task_id=task_id,
            subtask_id="s1",
            node_id="n1",
            backend_id="codex_sdk",
            attempt_id=1,
            started_at=now,
            finished_at=now,
            latency_seconds=0.5,
            prompt_tokens=40,
            completion_tokens=10,
            estimated_cost_usd=0.02,
            cost_quality="exact",
            accounting_source="hist",
            status="success",
            model_name="fake",
        ),
        BackendUsageRecord(
            usage_id="hist-b",
            task_id=task_id,
            subtask_id="s1",
            node_id="n2",
            backend_id="smolagents_code",
            attempt_id=1,
            started_at=now,
            finished_at=now,
            latency_seconds=0.3,
            prompt_tokens=20,
            completion_tokens=8,
            estimated_cost_usd=0.005,
            cost_quality="exact",
            accounting_source="hist",
            status="success",
            model_name="fake",
        ),
    ]
    state.public_evaluation_records = [
        PublicEvaluationRecord(
            evaluation_id="pub1",
            task_id=task_id,
            harness_id="repository_test_harness",
            visibility=EvaluationVisibility.PUBLIC,
            passed=True,
            normalized_score=0.9,
        )
    ]
    return plan, state, store


def _pareto_policy(tmp_path: Path) -> ParetoGlobalCandidatePolicy:
    return ParetoGlobalCandidatePolicy(
        config=ParetoConfig(
            enabled=True,
            max_candidates=8,
            fallback_to_rule_based=False,
            objectives=OBJ,
        ),
        preference_profile=PreferenceProfile(profile_id="balanced_knee"),
        run_dir=str(tmp_path),
    )


def test_scheduling_only_candidate_rejected_under_required_rule_block():
    plan = _missing_rule_plan("blk")
    state = TaskExecutionState.from_plan(plan)
    state.communication_plan = plan.communication_plan
    state.subtasks["s1"].status = SubtaskStatus.COMMITTED
    state.subtasks["s2"].status = SubtaskStatus.READY
    state.subtasks["s2"].communication_block_reason = (
        DeliveryFailureReason.REQUIRED_RULE_MISSING.value
    )
    edits = [SchedulingConcurrencyEdit(max_concurrent_subtasks=1)]
    new_plan, new_comm, new_policy, _ = apply_global_edits(
        task_plan=plan,
        communication_plan=state.communication_plan,
        scheduling_policy=TaskSchedulingPolicy(max_concurrent_subtasks=2),
        edits=edits,
        eligible_subtask_ids={"s2", "s3"},
    )
    g = GlobalCandidate(
        candidate_id="sched-only",
        diagnosis=GlobalDiagnosis(
            reasons=[GlobalDiagnosisReason.DELIVERY_FAILURE],
            affected_future_subtask_ids=["s2"],
            update_required=True,
            concise_explanation="block",
        ),
        edits=edits,
        proposed_task_plan=new_plan,
        proposed_communication_plan=new_comm,
        proposed_scheduling_policy=new_policy,
        validation_status=GlobalCandidateValidationStatus.VALID,
    )
    ctx = build_decision_context(
        parent_plan_hash=plan.content_hash(),
        parent_communication_hash="",
        committed_prefix=["s1"],
        eligible_future_subtask_ids={"s2", "s3"},
        triggers=[],
        diagnosis=g.diagnosis,
        preference_profile=PreferenceProfile(),
        backend_capabilities={},
        state=state,
        objective_config=OBJ,
        communication_plan=state.communication_plan,
    )
    pareto_cand = build_pareto_candidate(global_candidate=g, context=ctx)
    result = ParetoCandidateValidator().validate(
        pareto_cand, current_state=state, leased_subtask_ids=set()
    )
    assert result.ok is False
    assert any("REQUIRED_BLOCK_UNRESOLVED" in e for e in result.errors)
    ok, errors = candidate_resolves_active_required_blocks(
        state=state, proposed_communication_plan=new_comm
    )
    assert ok is False
    assert errors


@pytest.mark.asyncio
async def test_no_complete_frontier_still_applies_m5_safety_repair(tmp_path: Path):
    plan, state, _store = await _seed(tmp_path, task_id="m5m6_nof")
    state.subtasks["s2"].communication_block_reason = (
        DeliveryFailureReason.REQUIRED_RULE_MISSING.value
    )
    assert active_required_communication_blocks(state)
    policy = _pareto_policy(tmp_path)
    # Force empty propose so Pareto has no complete frontier.
    policy.propose = lambda **kwargs: []  # type: ignore[method-assign]
    ctrl = SlowLoopController(
        config=SlowLoopConfig(
            enabled=True,
            budget=SlowLoopBudget(min_commits_between_updates=0),
        ),
        candidate_policy=policy,
        checkpoint_store=TaskCheckpointStore(tmp_path),
    )
    result = await ctrl.maybe_update(
        task_plan=plan,
        state=state,
        context=_ctx(tmp_path, "m5m6_nof"),
        leased_subtask_ids=set(),
    )
    assert result.message != "NO_COMPARABLE_PARETO_CANDIDATE"
    assert result.updated is True
    assert state.active_plan_revision_id is not None
    applied = [
        r
        for r in state.plan_revision_history
        if r.status is GlobalPlanRevisionStatus.APPLIED
    ]
    assert len(applied) == 1
    assert any(isinstance(e, UpsertDeliveryRuleEdit) for e in applied[0].edits)
    assert any(
        isinstance(e, UpsertDeliveryRuleEdit) and e.rule.payload_id == "p1"
        for e in applied[0].edits
    )


@pytest.mark.asyncio
async def test_run_task_pareto_enabled_repairs_missing_rule_and_commits(tmp_path: Path):
    plan, state, store = await _seed(tmp_path, task_id="m5m6_e2e")
    ckpt = TaskCheckpointStore(tmp_path)
    policy = _pareto_policy(tmp_path)
    assert policy.config.fallback_to_rule_based is False
    ctrl = SlowLoopController(
        config=SlowLoopConfig(
            enabled=True,
            budget=SlowLoopBudget(
                min_commits_between_updates=0,
                max_candidates_per_update=8,
            ),
            allowed_backend_assignments={
                "coding": ["codex_sdk", "smolagents_code"],
            },
        ),
        candidate_policy=policy,
        checkpoint_store=ckpt,
    )
    runtime = AsyncMock()
    runtime.artifact_store = store
    sched = ReadySubtaskScheduler(
        runtime=runtime,
        artifact_store=store,
        task_checkpoint_store=ckpt,
        contracts_dir="configs/contracts",
        slow_loop=ctrl,
        max_concurrent_subtasks=1,
    )
    sched.input_assembler = SubtaskInputAssembler(store)

    # Confirm preflight blocks before recovery.
    deliverable = await sched._deliverable_ready_ids(
        state=state, task_plan=plan, initial_artifacts=ArtifactBundle()
    )
    assert deliverable == []
    assert state.subtasks["s2"].communication_block_reason == (
        DeliveryFailureReason.REQUIRED_RULE_MISSING.value
    )

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
        return SubtaskExecutionResult(
            subtask_id=sid,
            expected_state_version=kwargs.get("expected_state_version", 0),
            local_subtask_state=live,
            produced_artifacts=[art],
            execution_status=SubtaskExecutionStatus.SUCCESS_PENDING_COMMIT,
            candidate_harness_passed=True,
        )

    sched._run_subtask_isolated = _stub_run  # type: ignore[method-assign]
    out = await sched.run_task(
        plan,
        state,
        initial_artifacts=ArtifactBundle(),
        context=_ctx(tmp_path, "m5m6_e2e"),
    )
    assert out.subtasks["s2"].status is SubtaskStatus.COMMITTED
    applied = [
        r
        for r in out.plan_revision_history
        if r.status is GlobalPlanRevisionStatus.APPLIED
    ]
    # Exactly one communication safety revision for the missing rule; no
    # irrelevant scheduling-only revisions consumed while blocked.
    assert len(applied) == 1
    edits = list(applied[0].edits)
    assert any(isinstance(e, UpsertDeliveryRuleEdit) for e in edits)
    assert any(
        isinstance(e, UpsertDeliveryRuleEdit) and e.rule.payload_id == "p1"
        for e in edits
    )
    assert not any(
        getattr(e, "type", "")
        in {
            "scheduling_concurrency",
            "serialization_group",
            "pending_backend_assignment",
            "pending_priority",
            "context_budget",
        }
        for e in edits
    )
    # Rule present on the live communication plan.
    assert any(r.payload_id == "p1" for r in out.communication_plan.delivery_schedule)


def test_rule_based_generator_still_emits_exact_payload_rule():
    plan = _missing_rule_plan("gen")
    state = TaskExecutionState.from_plan(plan)
    state.communication_plan = plan.communication_plan
    state.subtasks["s1"].status = SubtaskStatus.COMMITTED
    state.subtasks["s2"].status = SubtaskStatus.READY
    state.subtasks["s2"].communication_block_reason = (
        DeliveryFailureReason.REQUIRED_RULE_MISSING.value
    )
    gen = RuleBasedGlobalCandidateGenerator(SlowLoopConfig(enabled=True))
    cands = gen.generate(
        task_plan=plan,
        communication_plan=plan.communication_plan,
        scheduling_policy=TaskSchedulingPolicy(),
        state=state,
        observation=GlobalObservation(
            task_id="gen",
            state_version=1,
            active_plan_version=1,
            active_communication_version=1,
        ),
        diagnosis=GlobalDiagnosis(
            reasons=[
                GlobalDiagnosisReason.DELIVERY_FAILURE,
                GlobalDiagnosisReason.MISSING_PAYLOAD,
            ],
            affected_future_subtask_ids=["s2"],
            update_required=True,
            concise_explanation="missing rule",
            recommended_edit_types=["upsert_delivery_rule"],
        ),
        eligible={"s2", "s3"},
    )
    comm = next(c for c in cands if c.candidate_id == "cand_comm")
    assert any(
        isinstance(e, UpsertDeliveryRuleEdit) and e.rule.payload_id == "p1"
        for e in comm.edits
    )
    ok, _ = candidate_resolves_active_required_blocks(
        state=state,
        proposed_communication_plan=comm.proposed_communication_plan,
    )
    assert ok is True


async def _seed_disabled_rule_with_pressure(
    tmp_path: Path, *, task_id: str
) -> tuple[TaskPlan, TaskExecutionState, FileArtifactStore]:
    store = FileArtifactStore(tmp_path / "artifacts")
    plan = TaskPlan(
        task_id=task_id,
        plan_version=1,
        decomposition_rationale="disabled rule + pressure",
        subtasks=[
            SubtaskSpec(
                subtask_id="s1",
                title="s1",
                objective="src",
                dependencies=[],
                keystone_harness_id="repository_test_harness",
                local_graph_template=GRAPH,
                budget=BudgetSpec(),
            ),
            SubtaskSpec(
                subtask_id="s2",
                title="s2",
                objective="blocked",
                dependencies=["s1"],
                keystone_harness_id="repository_test_harness",
                local_graph_template=GRAPH,
                budget=BudgetSpec(),
            ),
        ],
        communication_plan=CommunicationPlan(
            version=1,
            payload_contracts=[
                PayloadContract(
                    payload_id="p1",
                    source_subtask_id="s1",
                    target_subtask_id="s2",
                    artifact_type="FinalAnswerArtifact",
                    required=True,
                    max_tokens=256,
                ),
                PayloadContract(
                    payload_id="opt",
                    source_subtask_id="s1",
                    target_subtask_id="s2",
                    artifact_type="FinalAnswerArtifact",
                    required=False,
                    max_tokens=8000,
                    metadata={"priority": 90},
                ),
            ],
            delivery_schedule=[
                DeliveryRule(rule_id="r1", payload_id="p1", enabled=False),
                DeliveryRule(rule_id="r_opt", payload_id="opt", enabled=True),
            ],
            context_budgets={"s2": 1200},
        ),
        metadata={
            "allowed_backend_assignments": {
                "coding": ["codex_sdk", "smolagents_code"],
                "s2": ["codex_sdk", "smolagents_code"],
            }
        },
    )
    state = TaskExecutionState.from_plan(plan)
    state.communication_plan = plan.communication_plan
    state.scheduling_policy = TaskSchedulingPolicy(max_concurrent_subtasks=2)
    art = create_artifact(
        FinalAnswerArtifact(answer="upstream", source_node="s1"),
        producer_node_id="s1",
        task_id=task_id,
    )
    await store.put(art)
    state.subtasks["s1"].status = SubtaskStatus.COMMITTED
    state.subtasks["s1"].final_output_artifact_id = art.artifact_id
    state.subtasks["s2"].status = SubtaskStatus.READY
    state.committed_subtask_count = 1
    now = datetime.now(UTC)
    state.backend_usage_records = [
        BackendUsageRecord(
            usage_id="hist-a",
            task_id=task_id,
            subtask_id="s1",
            node_id="n1",
            backend_id="codex_sdk",
            attempt_id=1,
            started_at=now,
            finished_at=now,
            latency_seconds=0.5,
            prompt_tokens=40,
            completion_tokens=10,
            estimated_cost_usd=0.02,
            cost_quality="exact",
            accounting_source="hist",
            status="success",
            model_name="fake",
        )
    ]
    state.public_evaluation_records = [
        PublicEvaluationRecord(
            evaluation_id="pub1",
            task_id=task_id,
            harness_id="repository_test_harness",
            visibility=EvaluationVisibility.PUBLIC,
            passed=True,
            normalized_score=0.9,
        )
    ]
    return plan, state, store


async def _run_disabled_rule_recovery(
    tmp_path: Path,
    *,
    task_id: str,
    with_pareto: bool,
) -> TaskExecutionState:
    plan, state, store = await _seed_disabled_rule_with_pressure(
        tmp_path, task_id=task_id
    )
    ckpt = TaskCheckpointStore(tmp_path)
    if with_pareto:
        policy = _pareto_policy(tmp_path)
        assert policy.config.fallback_to_rule_based is False
        slow = SlowLoopController(
            config=SlowLoopConfig(
                enabled=True,
                budget=SlowLoopBudget(
                    min_commits_between_updates=0,
                    context_pressure_ratio=0.5,
                    max_candidates_per_update=8,
                ),
                allowed_backend_assignments={
                    "coding": ["codex_sdk", "smolagents_code"],
                },
            ),
            candidate_policy=policy,
            checkpoint_store=ckpt,
        )
    else:
        slow = SlowLoopController(
            config=SlowLoopConfig(
                enabled=True,
                budget=SlowLoopBudget(
                    min_commits_between_updates=0,
                    context_pressure_ratio=0.5,
                ),
            ),
            checkpoint_store=ckpt,
        )
    runtime = AsyncMock()
    runtime.artifact_store = store
    sched = ReadySubtaskScheduler(
        runtime=runtime,
        artifact_store=store,
        task_checkpoint_store=ckpt,
        contracts_dir="configs/contracts",
        slow_loop=slow,
        max_concurrent_subtasks=1,
    )
    sched.input_assembler = SubtaskInputAssembler(store)

    deliverable = await sched._deliverable_ready_ids(
        state=state, task_plan=plan, initial_artifacts=ArtifactBundle()
    )
    assert deliverable == []
    assert state.subtasks["s2"].communication_block_reason == (
        DeliveryFailureReason.REQUIRED_RULE_MISSING.value
    )

    async def _stub_run(**kwargs):  # noqa: ANN003
        sid = kwargs["subtask_id"]
        snapshot: TaskExecutionState = kwargs["state"]
        art = create_artifact(
            FinalAnswerArtifact(answer=f"out-{sid}", source_node=sid),
            producer_node_id=sid,
            task_id=plan.task_id,
        )
        await store.put(art)
        live = snapshot.subtasks[sid].model_copy(deep=True)
        live.status = SubtaskStatus.AWAITING_CANONICAL_COMMIT
        live.final_output_artifact_id = art.artifact_id
        live.communication_block_reason = None
        return SubtaskExecutionResult(
            subtask_id=sid,
            expected_state_version=kwargs.get("expected_state_version", 0),
            local_subtask_state=live,
            produced_artifacts=[art],
            execution_status=SubtaskExecutionStatus.SUCCESS_PENDING_COMMIT,
            candidate_harness_passed=True,
        )

    sched._run_subtask_isolated = _stub_run  # type: ignore[method-assign]
    return await sched.run_task(
        plan,
        state,
        initial_artifacts=ArtifactBundle(),
        context=_ctx(tmp_path, task_id),
    )


@pytest.mark.asyncio
async def test_disabled_rule_plus_pressure_enables_exact_rule_rule_based(tmp_path: Path):
    out = await _run_disabled_rule_recovery(
        tmp_path, task_id="m5_dis_rb", with_pareto=False
    )
    assert out.subtasks["s2"].status is SubtaskStatus.COMMITTED
    applied = [
        r
        for r in out.plan_revision_history
        if r.status is GlobalPlanRevisionStatus.APPLIED
    ]
    assert len(applied) == 1
    edits = list(applied[0].edits)
    assert any(
        isinstance(e, UpsertDeliveryRuleEdit)
        and e.rule.payload_id == "p1"
        and e.rule.enabled
        for e in edits
    )
    assert not any(
        getattr(e, "type", "")
        in {
            "scheduling_concurrency",
            "serialization_group",
            "pending_backend_assignment",
            "pending_priority",
        }
        for e in edits
    )
    enabled = [
        r
        for r in out.communication_plan.delivery_schedule
        if r.payload_id == "p1" and r.enabled
    ]
    assert enabled


@pytest.mark.asyncio
async def test_disabled_rule_plus_pressure_with_pareto_no_fallback(tmp_path: Path):
    out = await _run_disabled_rule_recovery(
        tmp_path, task_id="m5_dis_p", with_pareto=True
    )
    assert out.subtasks["s2"].status is SubtaskStatus.COMMITTED
    applied = [
        r
        for r in out.plan_revision_history
        if r.status is GlobalPlanRevisionStatus.APPLIED
    ]
    assert len(applied) == 1
    assert any(
        isinstance(e, UpsertDeliveryRuleEdit)
        and e.rule.payload_id == "p1"
        and e.rule.enabled
        for e in applied[0].edits
    )
    assert not any(
        getattr(e, "type", "") == "scheduling_concurrency" for e in applied[0].edits
    )
