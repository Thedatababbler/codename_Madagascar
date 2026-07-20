"""M5.3: immutable communication history and observation watermarks."""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestra.communication.aggregation import AggregationRule, AggregationStrategy
from orchestra.communication.delivery import CommunicationDeliveryEngine
from orchestra.communication.delta import (
    CommunicationTargetResolver,
    diff_communication_plans,
    immutable_communication_targets,
)
from orchestra.communication.ledger import (
    DeliveryFailureReason,
    DeliveryRecord,
    DeliveryStatus,
)
from orchestra.communication.payload import DeliveryRule, PayloadContract
from orchestra.communication.plan import CommunicationPlan
from orchestra.communication.validation import (
    CommunicationPlanValidationError,
    CommunicationValidationMode,
    validate_communication_plan,
)
from orchestra.control.slow_loop.diagnosis import detect_triggers, diagnose
from orchestra.control.slow_loop.edits import apply_global_edits
from orchestra.control.slow_loop.observation import (
    advance_observation_watermark,
    build_global_observation,
)
from orchestra.control.slow_loop.schemas import (
    AggregationRuleEdit,
    ContextBudgetEdit,
    GlobalDiagnosisReason,
    RemovePayloadContractEdit,
    SlowLoopBudget,
    SlowLoopState,
    SlowLoopTriggerReason,
    TaskSchedulingPolicy,
    UpsertDeliveryRuleEdit,
)
from orchestra.control.task_state import SubtaskStatus, TaskExecutionState
from orchestra.decomposition.schemas import BudgetSpec, SubtaskSpec, TaskPlan
from orchestra.ir.artifacts import create_artifact
from orchestra.schemas.artifacts import FinalAnswerArtifact
from orchestra.storage.artifacts import FileArtifactStore

GRAPH = "configs/graphs/codex_single_implementer.yaml"


def _plan() -> TaskPlan:
    return TaskPlan(
        task_id="m53",
        plan_version=1,
        decomposition_rationale="m53",
        subtasks=[
            SubtaskSpec(
                subtask_id="s1",
                title="s1",
                objective="a",
                dependencies=[],
                keystone_harness_id="h",
                local_graph_template=GRAPH,
                budget=BudgetSpec(),
            ),
            SubtaskSpec(
                subtask_id="s2",
                title="s2",
                objective="b",
                dependencies=["s1"],
                keystone_harness_id="h",
                local_graph_template=GRAPH,
                budget=BudgetSpec(),
            ),
            SubtaskSpec(
                subtask_id="s3",
                title="s3",
                objective="c",
                dependencies=["s2"],
                keystone_harness_id="h",
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
                    required=True,
                    max_tokens=256,
                    metadata={"slot": "comm:p12"},
                ),
                PayloadContract(
                    payload_id="p23",
                    source_subtask_id="s2",
                    target_subtask_id="s3",
                    artifact_type="FinalAnswerArtifact",
                    required=True,
                    max_tokens=256,
                    metadata={"slot": "comm:p23"},
                ),
            ],
            delivery_schedule=[
                DeliveryRule(rule_id="r12", payload_id="p12", enabled=True),
                DeliveryRule(rule_id="r23", payload_id="p23", enabled=True),
            ],
            aggregation_rules=[
                AggregationRule(
                    rule_id="agg2",
                    source_payload_ids=["p12"],
                    strategy=AggregationStrategy.LIST,
                    target_slot="comm:p12",
                    metadata={"target_subtask_id": "s2", "slot": "comm:p12"},
                )
            ],
            context_budgets={"s2": 10_000, "s3": 10_000},
        ),
    )


def _committed_s2_state() -> tuple[TaskPlan, TaskExecutionState]:
    plan = _plan()
    state = TaskExecutionState.from_plan(plan)
    state.communication_plan = plan.communication_plan
    state.subtasks["s1"].status = SubtaskStatus.COMMITTED
    state.subtasks["s2"].status = SubtaskStatus.COMMITTED
    state.subtasks["s3"].status = SubtaskStatus.PENDING
    return plan, state


def test_diff_communication_plans_detects_all_entities():
    plan = _plan()
    parent = plan.communication_plan
    proposed = parent.model_copy(deep=True)
    proposed = CommunicationPlan(
        version=2,
        payload_contracts=[
            c.model_copy(update={"max_tokens": 64}) if c.payload_id == "p12" else c
            for c in parent.payload_contracts
        ],
        delivery_schedule=[
            r.model_copy(update={"enabled": False}) if r.rule_id == "r12" else r
            for r in parent.delivery_schedule
        ],
        aggregation_rules=list(parent.aggregation_rules),
        context_budgets={"s2": 1, "s3": 10_000},
    )
    delta = diff_communication_plans(parent, proposed)
    assert "p12" in delta.changed_payload_ids
    assert "r12" in delta.changed_rule_ids
    assert "s2" in delta.changed_context_budget_targets


def test_completed_target_payload_change_rejected():
    plan, state = _committed_s2_state()
    parent = state.communication_plan
    proposed = CommunicationPlan(
        version=2,
        payload_contracts=[
            c.model_copy(update={"max_tokens": 1}) if c.payload_id == "p12" else c
            for c in parent.payload_contracts
        ],
        delivery_schedule=list(parent.delivery_schedule),
        aggregation_rules=list(parent.aggregation_rules),
        context_budgets=dict(parent.context_budgets),
    )
    with pytest.raises(CommunicationPlanValidationError, match="payload_contract p12"):
        validate_communication_plan(
            task_plan=plan,
            communication_plan=proposed,
            mode=CommunicationValidationMode.PROPOSED_REVISION,
            current_state=state,
            parent_communication_plan=parent,
        )


def test_completed_target_payload_removal_rejected():
    plan, state = _committed_s2_state()
    parent = state.communication_plan
    proposed = CommunicationPlan(
        version=2,
        payload_contracts=[c for c in parent.payload_contracts if c.payload_id != "p12"],
        delivery_schedule=[d for d in parent.delivery_schedule if d.payload_id != "p12"],
        aggregation_rules=[],
        context_budgets=dict(parent.context_budgets),
    )
    with pytest.raises(CommunicationPlanValidationError, match="IMMUTABLE"):
        validate_communication_plan(
            task_plan=plan,
            communication_plan=proposed,
            mode=CommunicationValidationMode.PROPOSED_REVISION,
            current_state=state,
            parent_communication_plan=parent,
        )


def test_completed_target_delivery_rule_change_rejected():
    plan, state = _committed_s2_state()
    parent = state.communication_plan
    proposed = CommunicationPlan(
        version=2,
        payload_contracts=list(parent.payload_contracts),
        delivery_schedule=[
            r.model_copy(update={"enabled": False}) if r.rule_id == "r12" else r
            for r in parent.delivery_schedule
        ],
        aggregation_rules=list(parent.aggregation_rules),
        context_budgets=dict(parent.context_budgets),
    )
    with pytest.raises(CommunicationPlanValidationError, match="delivery_rule r12"):
        validate_communication_plan(
            task_plan=plan,
            communication_plan=proposed,
            mode=CommunicationValidationMode.PROPOSED_REVISION,
            current_state=state,
            parent_communication_plan=parent,
        )


def test_completed_target_delivery_rule_removal_rejected():
    plan, state = _committed_s2_state()
    parent = state.communication_plan
    proposed = CommunicationPlan(
        version=2,
        payload_contracts=list(parent.payload_contracts),
        delivery_schedule=[r for r in parent.delivery_schedule if r.rule_id != "r12"],
        aggregation_rules=list(parent.aggregation_rules),
        context_budgets=dict(parent.context_budgets),
    )
    with pytest.raises(CommunicationPlanValidationError, match="delivery_rule r12"):
        validate_communication_plan(
            task_plan=plan,
            communication_plan=proposed,
            mode=CommunicationValidationMode.PROPOSED_REVISION,
            current_state=state,
            parent_communication_plan=parent,
        )


def test_completed_target_aggregation_change_rejected():
    plan, state = _committed_s2_state()
    parent = state.communication_plan
    new_agg = parent.aggregation_rules[0].model_copy(
        update={"max_tokens": 1}
    )
    proposed = CommunicationPlan(
        version=2,
        payload_contracts=list(parent.payload_contracts),
        delivery_schedule=list(parent.delivery_schedule),
        aggregation_rules=[new_agg],
        context_budgets=dict(parent.context_budgets),
    )
    with pytest.raises(CommunicationPlanValidationError, match="aggregation_rule"):
        validate_communication_plan(
            task_plan=plan,
            communication_plan=proposed,
            mode=CommunicationValidationMode.PROPOSED_REVISION,
            current_state=state,
            parent_communication_plan=parent,
        )


def test_completed_target_context_budget_change_rejected():
    plan, state = _committed_s2_state()
    parent = state.communication_plan
    proposed = CommunicationPlan(
        version=2,
        payload_contracts=list(parent.payload_contracts),
        delivery_schedule=list(parent.delivery_schedule),
        aggregation_rules=list(parent.aggregation_rules),
        context_budgets={**parent.context_budgets, "s2": 1},
    )
    with pytest.raises(CommunicationPlanValidationError, match="context_budget"):
        validate_communication_plan(
            task_plan=plan,
            communication_plan=proposed,
            mode=CommunicationValidationMode.PROPOSED_REVISION,
            current_state=state,
            parent_communication_plan=parent,
        )


def test_leased_target_communication_change_rejected():
    plan = _plan()
    state = TaskExecutionState.from_plan(plan)
    state.communication_plan = plan.communication_plan
    state.subtasks["s2"].status = SubtaskStatus.READY
    state.subtasks["s2"].lease_status = "leased"
    assert "s2" in immutable_communication_targets(state)
    parent = state.communication_plan
    proposed = CommunicationPlan(
        version=2,
        payload_contracts=[
            c.model_copy(update={"max_tokens": 8}) if c.payload_id == "p12" else c
            for c in parent.payload_contracts
        ],
        delivery_schedule=list(parent.delivery_schedule),
        aggregation_rules=list(parent.aggregation_rules),
        context_budgets=dict(parent.context_budgets),
    )
    with pytest.raises(CommunicationPlanValidationError, match="IMMUTABLE"):
        validate_communication_plan(
            task_plan=plan,
            communication_plan=proposed,
            mode=CommunicationValidationMode.PROPOSED_REVISION,
            current_state=state,
            parent_communication_plan=parent,
        )


def test_running_target_communication_change_rejected():
    plan = _plan()
    state = TaskExecutionState.from_plan(plan)
    state.communication_plan = plan.communication_plan
    state.subtasks["s2"].status = SubtaskStatus.RUNNING
    parent = state.communication_plan
    _, _, _, rejected = apply_global_edits(
        task_plan=plan,
        communication_plan=parent,
        scheduling_policy=TaskSchedulingPolicy(),
        edits=[
            UpsertDeliveryRuleEdit(
                rule=DeliveryRule(rule_id="r12", payload_id="p12", enabled=False)
            )
        ],
        eligible_subtask_ids={"s3"},
    )
    assert "upsert_delivery_rule" in rejected


def test_apply_rejects_remove_and_aggregation_for_completed():
    plan, state = _committed_s2_state()
    _, _, _, rejected = apply_global_edits(
        task_plan=plan,
        communication_plan=state.communication_plan,
        scheduling_policy=TaskSchedulingPolicy(),
        edits=[
            RemovePayloadContractEdit(payload_id="p12"),
            AggregationRuleEdit(
                rule=AggregationRule(
                    rule_id="agg2",
                    source_payload_ids=["p12"],
                    strategy=AggregationStrategy.LIST,
                    target_slot="comm:p12",
                    max_tokens=1,
                    metadata={"target_subtask_id": "s2", "slot": "comm:p12"},
                )
            ),
            ContextBudgetEdit(target_subtask_id="s2", max_tokens=1),
        ],
        eligible_subtask_ids={"s3"},
    )
    assert "remove_payload_contract" in rejected
    assert "aggregation_rule" in rejected
    assert "context_budget" in rejected


@pytest.mark.asyncio
async def test_delivery_engine_rejects_completed_target(tmp_path: Path):
    store = FileArtifactStore(tmp_path)
    plan, state = _committed_s2_state()
    result = await CommunicationDeliveryEngine(store).deliver_for_target(
        task_plan=plan,
        task_state=state,
        communication_plan=state.communication_plan,
        target_subtask_id="s2",
    )
    assert result.blocked is True
    assert result.block_reason is DeliveryFailureReason.TARGET_NOT_DELIVERABLE
    assert result.delivered_slots == {}
    assert result.new_records == []
    assert result.projections == []


@pytest.mark.asyncio
async def test_delivery_engine_rejects_leased_target(tmp_path: Path):
    """LEASED+RUNNING (past assembly) is not deliverable; READY+leased is assembly."""
    store = FileArtifactStore(tmp_path)
    plan = _plan()
    state = TaskExecutionState.from_plan(plan)
    state.communication_plan = plan.communication_plan
    state.subtasks["s2"].status = SubtaskStatus.RUNNING
    state.subtasks["s2"].lease_status = "leased"
    result = await CommunicationDeliveryEngine(store).deliver_for_target(
        task_plan=plan,
        task_state=state,
        communication_plan=state.communication_plan,
        target_subtask_id="s2",
    )
    assert result.block_reason is DeliveryFailureReason.TARGET_NOT_DELIVERABLE


@pytest.mark.asyncio
async def test_delivery_engine_rejects_running_target(tmp_path: Path):
    store = FileArtifactStore(tmp_path)
    plan = _plan()
    state = TaskExecutionState.from_plan(plan)
    state.communication_plan = plan.communication_plan
    state.subtasks["s2"].status = SubtaskStatus.RUNNING
    result = await CommunicationDeliveryEngine(store).deliver_for_target(
        task_plan=plan,
        task_state=state,
        communication_plan=state.communication_plan,
        target_subtask_id="s2",
    )
    assert result.block_reason is DeliveryFailureReason.TARGET_NOT_DELIVERABLE


@pytest.mark.asyncio
async def test_delivery_engine_allows_leased_ready_for_assembly(tmp_path: Path):
    """Scheduler leases before worker assembly; READY+leased must remain deliverable."""
    store = FileArtifactStore(tmp_path)
    plan = _plan()
    state = TaskExecutionState.from_plan(plan)
    state.communication_plan = plan.communication_plan
    art = create_artifact(
        FinalAnswerArtifact(answer="x", source_node="s1"),
        producer_node_id="s1",
        task_id="m53",
    )
    await store.put(art)
    state.subtasks["s1"].status = SubtaskStatus.COMMITTED
    state.subtasks["s1"].final_output_artifact_id = art.artifact_id
    state.subtasks["s2"].status = SubtaskStatus.READY
    state.subtasks["s2"].lease_status = "leased"
    result = await CommunicationDeliveryEngine(store).deliver_for_target(
        task_plan=plan,
        task_state=state,
        communication_plan=state.communication_plan,
        target_subtask_id="s2",
    )
    assert result.block_reason is not DeliveryFailureReason.TARGET_NOT_DELIVERABLE


@pytest.mark.asyncio
async def test_completed_target_delivery_creates_no_projection(tmp_path: Path):
    store = FileArtifactStore(tmp_path)
    plan, state = _committed_s2_state()
    art = create_artifact(
        FinalAnswerArtifact(answer="x", source_node="s1"),
        producer_node_id="s1",
        task_id="m53",
    )
    await store.put(art)
    state.subtasks["s1"].final_output_artifact_id = art.artifact_id
    result = await CommunicationDeliveryEngine(store).deliver_for_target(
        task_plan=plan,
        task_state=state,
        communication_plan=state.communication_plan,
        target_subtask_id="s2",
        persist=True,
    )
    assert result.projections == []
    assert not any(r.status is DeliveryStatus.DELIVERED for r in result.new_records)
    assert not any(r.status is DeliveryStatus.DELIVERED for r in result.audit_records)


@pytest.mark.asyncio
async def test_completed_target_delivery_creates_no_delivered_record(tmp_path: Path):
    store = FileArtifactStore(tmp_path)
    plan, state = _committed_s2_state()
    result = await CommunicationDeliveryEngine(store).deliver_for_target(
        task_plan=plan,
        task_state=state,
        communication_plan=state.communication_plan,
        target_subtask_id="s2",
    )
    assert result.new_records == []
    assert all(
        r.status is DeliveryStatus.SKIPPED_TARGET_NOT_DELIVERABLE
        for r in result.audit_records
    )


def test_historical_context_pressure_does_not_affect_future_targets():
    plan, state = _committed_s2_state()
    # Oversized historical S2 budget contracts stay in plan.
    state.communication_plan = CommunicationPlan(
        version=1,
        payload_contracts=list(plan.communication_plan.payload_contracts),
        delivery_schedule=list(plan.communication_plan.delivery_schedule),
        aggregation_rules=list(plan.communication_plan.aggregation_rules),
        context_budgets={"s2": 10, "s3": 100_000},
    )
    obs = build_global_observation(state)
    assert "s2" not in obs.target_context_pressure
    assert "s3" in obs.target_context_pressure
    assert obs.target_context_pressure["s3"] < 0.9


def test_context_pressure_without_eligible_target_returns_no_safe_edit():
    plan = _plan()
    state = TaskExecutionState.from_plan(plan)
    state.communication_plan = plan.communication_plan
    # All futures leased → no eligible pressure targets in observation, but
    # synthesize a CONTEXT_PRESSURE trigger with empty affected.
    state.subtasks["s2"].status = SubtaskStatus.READY
    state.subtasks["s2"].lease_status = "leased"
    state.subtasks["s3"].status = SubtaskStatus.READY
    state.subtasks["s3"].lease_status = "leased"
    obs = build_global_observation(state)
    obs = obs.model_copy(
        update={"target_context_pressure": {"s2": 2.0}, "commits_since_last_slow_update": 0}
    )
    triggers = [SlowLoopTriggerReason.CONTEXT_PRESSURE]
    diag = diagnose(
        observation=obs,
        task_plan=plan,
        task_state=state,
        communication_plan=state.communication_plan,
        triggers=triggers,
        budget=SlowLoopBudget(context_pressure_ratio=0.9),
    )
    assert GlobalDiagnosisReason.NO_SAFE_FUTURE_EDIT in diag.reasons
    assert diag.affected_future_subtask_ids == []


def test_old_delivery_failure_does_not_retrigger():
    plan = _plan()
    state = TaskExecutionState.from_plan(plan)
    state.communication_plan = plan.communication_plan.model_copy(update={"version": 2})
    state.delivery_ledger.append(
        DeliveryRecord(
            delivery_id="old-fail",
            communication_plan_version=1,
            payload_id="p12",
            source_subtask_id="s1",
            target_subtask_id="s2",
            source_artifact_id="a",
            delivered_at_state_version=0,
            status=DeliveryStatus.FAILED,
            failure_reason=DeliveryFailureReason.REQUIRED_RULE_MISSING,
        )
    )
    state.slow_loop_state = SlowLoopState(last_observed_delivery_index=1)
    obs = build_global_observation(state)
    triggers = detect_triggers(
        obs, budget=SlowLoopBudget(min_commits_between_updates=99)
    )
    assert SlowLoopTriggerReason.DELIVERY_FAILURE not in triggers


def test_active_revision_delivery_failure_triggers():
    plan = _plan()
    state = TaskExecutionState.from_plan(plan)
    state.communication_plan = plan.communication_plan
    state.subtasks["s3"].status = SubtaskStatus.READY
    state.subtasks["s3"].communication_block_reason = (
        DeliveryFailureReason.REQUIRED_RULE_MISSING.value
    )
    obs = build_global_observation(state)
    triggers = detect_triggers(
        obs, budget=SlowLoopBudget(min_commits_between_updates=99)
    )
    assert SlowLoopTriggerReason.DELIVERY_FAILURE in triggers


def test_handled_harness_failure_does_not_retrigger():
    plan = _plan()
    state = TaskExecutionState.from_plan(plan)
    from orchestra.control.task_state import SubtaskAttempt, SubtaskFailureReason

    state.subtasks["s1"].status = SubtaskStatus.HARNESS_FAILED
    state.subtasks["s1"].failure_reason = SubtaskFailureReason.HARNESS
    state.subtasks["s1"].attempts = [
        SubtaskAttempt(attempt_id=1, status=SubtaskStatus.HARNESS_FAILED)
    ]
    from orchestra.control.slow_loop.evidence import harness_evidence_key

    key = harness_evidence_key(
        subtask_id="s1",
        attempt_id=1,
        harness_artifact_id="",
        failure_reason=SubtaskFailureReason.HARNESS.value,
    )
    state.slow_loop_state = SlowLoopState(
        handled_evidence_keys=[key],
        last_observed_delivery_index=0,
    )
    obs = build_global_observation(state)
    assert obs.recent_harness_failures == 0


def test_new_harness_failure_after_watermark_triggers():
    plan = _plan()
    state = TaskExecutionState.from_plan(plan)
    from orchestra.control.task_state import SubtaskAttempt, SubtaskFailureReason

    state.subtasks["s1"].status = SubtaskStatus.HARNESS_FAILED
    state.subtasks["s1"].failure_reason = SubtaskFailureReason.HARNESS
    state.subtasks["s1"].attempts = [
        SubtaskAttempt(attempt_id=1, status=SubtaskStatus.HARNESS_FAILED)
    ]
    state.subtasks["s2"].status = SubtaskStatus.HARNESS_FAILED
    state.subtasks["s2"].failure_reason = SubtaskFailureReason.HARNESS
    state.subtasks["s2"].attempts = [
        SubtaskAttempt(attempt_id=1, status=SubtaskStatus.HARNESS_FAILED)
    ]
    state.slow_loop_state = SlowLoopState(handled_evidence_keys=[])
    obs = build_global_observation(state)
    assert obs.recent_harness_failures >= 2
    triggers = detect_triggers(
        obs,
        budget=SlowLoopBudget(
            repeated_failure_threshold=2, min_commits_between_updates=99
        ),
    )
    assert SlowLoopTriggerReason.REPEATED_HARNESS_FAILURE in triggers


def test_watermark_advance_consumes_evidence():
    plan = _plan()
    state = TaskExecutionState.from_plan(plan)
    state.subtasks["s3"].status = SubtaskStatus.READY
    state.subtasks["s3"].communication_block_reason = (
        DeliveryFailureReason.REQUIRED_RULE_MISSING.value
    )
    obs1 = build_global_observation(state)
    assert SlowLoopTriggerReason.DELIVERY_FAILURE in detect_triggers(
        obs1, budget=SlowLoopBudget(min_commits_between_updates=99)
    )
    advance_observation_watermark(state, observation=obs1)
    # Block remains unresolved; fingerprint is handled → no retrigger.
    assert state.subtasks["s3"].communication_block_reason is not None
    obs2 = build_global_observation(state)
    assert SlowLoopTriggerReason.DELIVERY_FAILURE not in detect_triggers(
        obs2, budget=SlowLoopBudget(min_commits_between_updates=99)
    )


def test_resolver_delivery_and_aggregation_targets():
    plan = _plan()
    resolver = CommunicationTargetResolver()
    assert resolver.payload_target("p12", plan.communication_plan) == "s2"
    assert resolver.delivery_rule_target("r12", plan.communication_plan) == "s2"
    assert resolver.aggregation_rule_targets("agg2", plan.communication_plan) == {"s2"}
