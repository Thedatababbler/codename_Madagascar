"""Deterministic GlobalDiagnosis from GlobalObservation."""

from __future__ import annotations

from orchestra.communication.ledger import DeliveryFailureReason
from orchestra.communication.plan import CommunicationPlan
from orchestra.control.slow_loop.schemas import (
    GlobalDiagnosis,
    GlobalDiagnosisReason,
    GlobalObservation,
    SlowLoopBudget,
    SlowLoopTriggerReason,
)
from orchestra.control.task_state import SubtaskStatus, TaskExecutionState
from orchestra.decomposition.schemas import TaskPlan

AGGREGATION_FAILURE_REASONS = {
    DeliveryFailureReason.AGGREGATION_CONFLICT.value,
    DeliveryFailureReason.AGGREGATION_RULE_MISSING.value,
    DeliveryFailureReason.AMBIGUOUS_AGGREGATION_RULE.value,
    DeliveryFailureReason.AGGREGATION_INPUT_SET_MISMATCH.value,
    DeliveryFailureReason.AGGREGATION_REQUIRED_INPUT_MISSING.value,
}

REQUIRED_BLOCK_REASONS = {
    DeliveryFailureReason.REQUIRED_SOURCE_NOT_COMMITTED.value,
    DeliveryFailureReason.REQUIRED_ARTIFACT_MISSING.value,
    DeliveryFailureReason.REQUIRED_FIELD_MISSING.value,
    DeliveryFailureReason.REQUIRED_RULE_MISSING.value,
    DeliveryFailureReason.REQUIRED_CONDITION_UNSATISFIED.value,
    DeliveryFailureReason.CONTEXT_BUDGET_INFEASIBLE.value,
    DeliveryFailureReason.PROJECTION_INFEASIBLE.value,
    DeliveryFailureReason.LEDGER_CORRUPTION.value,
}


def detect_triggers(
    observation: GlobalObservation,
    *,
    budget: SlowLoopBudget,
) -> list[SlowLoopTriggerReason]:
    reasons: list[SlowLoopTriggerReason] = []
    if observation.commits_since_last_slow_update >= budget.min_commits_between_updates:
        reasons.append(SlowLoopTriggerReason.PERIODIC_COMMIT_CHECKPOINT)
    for _target, ratio in observation.target_context_pressure.items():
        if ratio >= budget.context_pressure_ratio:
            reasons.append(SlowLoopTriggerReason.CONTEXT_PRESSURE)
            break
    rem = observation.remaining_task_budget
    budget_signal = (
        rem.budget_configured
        or rem.max_backend_calls is not None
        or rem.max_cost_usd is not None
    )
    if budget_signal and rem.ratio < budget.budget_pressure_ratio:
        reasons.append(SlowLoopTriggerReason.BUDGET_PRESSURE)

    recent_backend = observation.recent_backend_failure_counts
    if (
        recent_backend
        and max(recent_backend.values()) >= budget.repeated_failure_threshold
    ):
        reasons.append(SlowLoopTriggerReason.REPEATED_BACKEND_FAILURE)
    if observation.recent_harness_failures >= budget.repeated_failure_threshold:
        reasons.append(SlowLoopTriggerReason.REPEATED_HARNESS_FAILURE)
    if observation.recent_canonical_conflicts > 0:
        reasons.append(SlowLoopTriggerReason.CANONICAL_CONFLICT)

    stats = observation.recent_delivery_statistics
    delivery_fail = (
        stats.failed_count > 0
        or stats.required_delivery_block_count > 0
        or any(stats.delivery_failure_counts.values())
    )
    if delivery_fail:
        reasons.append(SlowLoopTriggerReason.DELIVERY_FAILURE)
    if any(stats.aggregation_failure_counts.values()):
        reasons.append(SlowLoopTriggerReason.AGGREGATION_RISK)
    return reasons


def diagnose(
    *,
    observation: GlobalObservation,
    task_plan: TaskPlan,
    task_state: TaskExecutionState,
    communication_plan: CommunicationPlan,
    triggers: list[SlowLoopTriggerReason],
    budget: SlowLoopBudget | None = None,
) -> GlobalDiagnosis:
    del task_plan
    reasons: list[GlobalDiagnosisReason] = []
    affected: list[str] = []
    edit_types: list[str] = []
    threshold = budget.context_pressure_ratio if budget is not None else 0.9

    future_ids = [
        sid
        for sid, sub in task_state.subtasks.items()
        if sub.status in {SubtaskStatus.PENDING, SubtaskStatus.READY}
        and sub.lease_status == "unleased"
    ]

    if SlowLoopTriggerReason.CONTEXT_PRESSURE in triggers:
        reasons.append(GlobalDiagnosisReason.CONTEXT_PRESSURE)
        for target, ratio in observation.target_context_pressure.items():
            if ratio >= threshold and target in future_ids:
                affected.append(target)
        # No fallback to all future subtasks — pressure without eligible
        # affected target is handled as NO_SAFE_FUTURE_EDIT below.
        edit_types.extend(["upsert_payload_contract"])

    if SlowLoopTriggerReason.BUDGET_PRESSURE in triggers:
        reasons.append(GlobalDiagnosisReason.BUDGET_PRESSURE)
        affected.extend(future_ids)
        edit_types.extend(
            ["pending_backend_assignment", "scheduling_concurrency"]
        )

    if SlowLoopTriggerReason.REPEATED_BACKEND_FAILURE in triggers:
        reasons.append(GlobalDiagnosisReason.BACKEND_INSTABILITY)
        affected.extend(future_ids)
        edit_types.append("pending_backend_assignment")

    if SlowLoopTriggerReason.REPEATED_HARNESS_FAILURE in triggers:
        reasons.append(GlobalDiagnosisReason.HARNESS_INSTABILITY)
        reasons.append(GlobalDiagnosisReason.SCHEDULING_CONTENTION)
        affected.extend(future_ids)
        edit_types.extend(
            [
                "scheduling_concurrency",
                "serialization_group",
                "pending_backend_assignment",
            ]
        )

    if SlowLoopTriggerReason.CANONICAL_CONFLICT in triggers:
        reasons.append(GlobalDiagnosisReason.CANONICAL_CONFLICT_RISK)
        affected.extend(future_ids)
        edit_types.extend(["serialization_group", "scheduling_concurrency"])

    if SlowLoopTriggerReason.DELIVERY_FAILURE in triggers:
        reasons.append(GlobalDiagnosisReason.DELIVERY_FAILURE)
        stats = observation.recent_delivery_statistics
        fail_keys = set(stats.delivery_failure_counts)
        if fail_keys & {DeliveryFailureReason.CONTEXT_BUDGET_INFEASIBLE.value}:
            reasons.append(GlobalDiagnosisReason.CONTEXT_PRESSURE)
            edit_types.append("upsert_payload_contract")
            for target, ratio in observation.target_context_pressure.items():
                if ratio >= threshold and target in future_ids:
                    affected.append(target)
        if fail_keys & {
            DeliveryFailureReason.REQUIRED_RULE_MISSING.value,
            DeliveryFailureReason.REQUIRED_ARTIFACT_MISSING.value,
            DeliveryFailureReason.REQUIRED_SOURCE_NOT_COMMITTED.value,
        }:
            reasons.append(GlobalDiagnosisReason.MISSING_PAYLOAD)
            edit_types.extend(["upsert_payload_contract", "upsert_delivery_rule"])
        # Only blocked future targets are affected — not all futures.
        for sid in future_ids:
            if task_state.subtasks[sid].communication_block_reason:
                affected.append(sid)

    if SlowLoopTriggerReason.AGGREGATION_RISK in triggers:
        reasons.append(GlobalDiagnosisReason.AGGREGATION_RISK)
        for sid in future_ids:
            br = task_state.subtasks[sid].communication_block_reason
            if br and br in AGGREGATION_FAILURE_REASONS:
                affected.append(sid)
        edit_types.extend(["upsert_payload_contract"])

    covered_targets = {
        c.target_subtask_id for c in communication_plan.payload_contracts
    }
    for sid in future_ids:
        sub = task_state.subtasks[sid]
        needs = [r for r in sub.spec.input_artifacts if r.artifact_id == ""]
        if needs and sid not in covered_targets and sub.spec.dependencies:
            reasons.append(GlobalDiagnosisReason.MISSING_PAYLOAD)
            affected.append(sid)
            edit_types.extend(["upsert_payload_contract", "upsert_delivery_rule"])

    for sid in future_ids:
        sub = task_state.subtasks[sid]
        reason = sub.communication_block_reason
        if not reason:
            continue
        if reason in AGGREGATION_FAILURE_REASONS:
            reasons.append(GlobalDiagnosisReason.AGGREGATION_RISK)
            affected.append(sid)
        elif reason in REQUIRED_BLOCK_REASONS:
            reasons.append(GlobalDiagnosisReason.DELIVERY_FAILURE)
            if reason == DeliveryFailureReason.CONTEXT_BUDGET_INFEASIBLE.value:
                reasons.append(GlobalDiagnosisReason.CONTEXT_PRESSURE)
            else:
                reasons.append(GlobalDiagnosisReason.MISSING_PAYLOAD)
            affected.append(sid)

    affected = sorted(set(affected))
    edit_types = sorted(set(edit_types))
    reasons = list(dict.fromkeys(reasons))
    if not reasons:
        reasons = [GlobalDiagnosisReason.NO_CHANGE]

    update_required = GlobalDiagnosisReason.NO_CHANGE not in reasons or len(reasons) > 1
    if reasons == [GlobalDiagnosisReason.NO_CHANGE]:
        update_required = False
    if triggers == [SlowLoopTriggerReason.PERIODIC_COMMIT_CHECKPOINT] and reasons == [
        GlobalDiagnosisReason.NO_CHANGE
    ]:
        update_required = False

    pressure_triggers = set(triggers) - {SlowLoopTriggerReason.PERIODIC_COMMIT_CHECKPOINT}
    if not pressure_triggers and GlobalDiagnosisReason.MISSING_PAYLOAD not in reasons:
        update_required = False
        reasons = [GlobalDiagnosisReason.NO_CHANGE]

    # CONTEXT_PRESSURE (or other pressure) with no eligible affected target.
    if (
        pressure_triggers
        and not affected
        and GlobalDiagnosisReason.MISSING_PAYLOAD not in reasons
    ):
        reasons = [GlobalDiagnosisReason.NO_SAFE_FUTURE_EDIT]
        update_required = True

    if pressure_triggers and reasons == [GlobalDiagnosisReason.NO_CHANGE]:
        reasons = [GlobalDiagnosisReason.NO_SAFE_FUTURE_EDIT]
        update_required = True

    explanation = (
        "no slow-loop update required"
        if not update_required
        else f"slow-loop recommended: {', '.join(r.value for r in reasons)}"
    )
    return GlobalDiagnosis(
        reasons=reasons,
        affected_future_subtask_ids=affected,
        evidence_artifact_ids=[],
        evidence_state_versions=[observation.state_version],
        recommended_edit_types=edit_types,
        concise_explanation=explanation,
        update_required=update_required,
    )
