"""Deterministic GlobalDiagnosis from GlobalObservation."""

from __future__ import annotations

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
    if observation.remaining_task_budget.ratio < budget.budget_pressure_ratio:
        reasons.append(SlowLoopTriggerReason.BUDGET_PRESSURE)
    if (
        observation.backend_failure_counts
        and max(observation.backend_failure_counts.values())
        >= budget.repeated_failure_threshold
    ):
        reasons.append(SlowLoopTriggerReason.REPEATED_BACKEND_FAILURE)
    if observation.harness_failures >= budget.repeated_failure_threshold:
        reasons.append(SlowLoopTriggerReason.REPEATED_HARNESS_FAILURE)
    if observation.canonical_merge_conflicts > 0:
        reasons.append(SlowLoopTriggerReason.CANONICAL_CONFLICT)
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
        if not affected:
            affected.extend(future_ids)
        edit_types.extend(["context_budget", "upsert_payload_contract"])

    if SlowLoopTriggerReason.BUDGET_PRESSURE in triggers:
        reasons.append(GlobalDiagnosisReason.BUDGET_PRESSURE)
        affected.extend(future_ids)
        edit_types.extend(
            ["pending_backend_assignment", "context_budget", "scheduling_concurrency"]
        )

    if SlowLoopTriggerReason.REPEATED_BACKEND_FAILURE in triggers:
        reasons.append(GlobalDiagnosisReason.BACKEND_INSTABILITY)
        affected.extend(future_ids)
        edit_types.append("pending_backend_assignment")

    if SlowLoopTriggerReason.CANONICAL_CONFLICT in triggers:
        reasons.append(GlobalDiagnosisReason.CANONICAL_CONFLICT_RISK)
        affected.extend(future_ids)
        edit_types.extend(["serialization_group", "scheduling_concurrency"])

    # Missing payload: future subtask with empty input selectors and no covering contract.
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

    affected = sorted(set(affected))
    edit_types = sorted(set(edit_types))
    if not reasons:
        reasons = [GlobalDiagnosisReason.NO_CHANGE]

    update_required = GlobalDiagnosisReason.NO_CHANGE not in reasons or len(reasons) > 1
    if reasons == [GlobalDiagnosisReason.NO_CHANGE]:
        update_required = False
    # Periodic checkpoint alone without other pressure → no update.
    if triggers == [SlowLoopTriggerReason.PERIODIC_COMMIT_CHECKPOINT] and reasons == [
        GlobalDiagnosisReason.NO_CHANGE
    ]:
        update_required = False

    # If we only have periodic + no diagnosis pressure, skip.
    pressure_triggers = set(triggers) - {SlowLoopTriggerReason.PERIODIC_COMMIT_CHECKPOINT}
    if not pressure_triggers and GlobalDiagnosisReason.MISSING_PAYLOAD not in reasons:
        update_required = False
        reasons = [GlobalDiagnosisReason.NO_CHANGE]

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
