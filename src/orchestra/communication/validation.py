"""CommunicationPlan structural validation (M5)."""

from __future__ import annotations

from orchestra.communication.plan import CommunicationPlan
from orchestra.decomposition.schemas import TaskPlan
from orchestra.ir.artifacts import PAYLOAD_SCHEMAS

PRIVATE_ARTIFACT_MARKERS = (
    "Private",
    "Hidden",
    "FinalLCB",
    "private_evaluator",
    "hidden_test",
)


class CommunicationPlanValidationError(ValueError):
    pass


def validate_communication_plan(
    *,
    task_plan: TaskPlan,
    communication_plan: CommunicationPlan,
    completed_subtask_ids: set[str] | None = None,
) -> None:
    completed = completed_subtask_ids or set()
    subtask_ids = {s.subtask_id for s in task_plan.subtasks}
    payload_ids: set[str] = set()

    for contract in communication_plan.payload_contracts:
        if contract.payload_id in payload_ids:
            raise CommunicationPlanValidationError(
                f"duplicate payload_id: {contract.payload_id}"
            )
        payload_ids.add(contract.payload_id)
        if contract.source_subtask_id not in subtask_ids:
            raise CommunicationPlanValidationError(
                f"payload {contract.payload_id}: unknown source {contract.source_subtask_id}"
            )
        if contract.target_subtask_id not in subtask_ids:
            raise CommunicationPlanValidationError(
                f"payload {contract.payload_id}: unknown target {contract.target_subtask_id}"
            )
        if contract.source_subtask_id == contract.target_subtask_id:
            raise CommunicationPlanValidationError(
                f"payload {contract.payload_id}: source equals target"
            )
        if contract.target_subtask_id in completed:
            raise CommunicationPlanValidationError(
                f"payload {contract.payload_id}: delivery targets completed "
                f"subtask {contract.target_subtask_id}"
            )
        if contract.max_tokens <= 0:
            raise CommunicationPlanValidationError(
                f"payload {contract.payload_id}: max_tokens must be > 0"
            )
        if any(m.lower() in contract.artifact_type.lower() for m in PRIVATE_ARTIFACT_MARKERS):
            raise CommunicationPlanValidationError(
                f"payload {contract.payload_id}: private/hidden artifact type forbidden"
            )
        if (
            contract.artifact_type not in PAYLOAD_SCHEMAS
            and not contract.artifact_type.endswith("Artifact")
        ):
            raise CommunicationPlanValidationError(
                f"payload {contract.payload_id}: illegal artifact_type {contract.artifact_type}"
            )

    for rule in communication_plan.delivery_schedule:
        if rule.payload_id not in payload_ids:
            raise CommunicationPlanValidationError(
                f"delivery rule {rule.rule_id}: unknown payload {rule.payload_id}"
            )
        if rule.forward_only is False:
            raise CommunicationPlanValidationError(
                f"delivery rule {rule.rule_id}: backward delivery forbidden in M5"
            )

    for sid, budget in communication_plan.context_budgets.items():
        if sid not in subtask_ids:
            raise CommunicationPlanValidationError(
                f"context budget for unknown subtask {sid}"
            )
        if budget < 0:
            raise CommunicationPlanValidationError(
                f"context budget for {sid} must be >= 0"
            )

    for agg in communication_plan.aggregation_rules:
        for src in agg.source_subtask_ids:
            if src not in subtask_ids:
                raise CommunicationPlanValidationError(
                    f"aggregation {agg.rule_id}: unknown source {src}"
                )
