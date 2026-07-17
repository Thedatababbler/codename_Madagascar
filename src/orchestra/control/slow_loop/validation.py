"""Future-only plan validation for Slow Loop revisions."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from orchestra.communication.compiler import communication_plan_hash
from orchestra.communication.validation import (
    CommunicationPlanValidationError,
    validate_communication_plan,
)
from orchestra.control.slow_loop.schemas import (
    GlobalEdit,
    PendingBackendAssignmentEdit,
    PendingGraphTemplateEdit,
    SlowLoopConfig,
)
from orchestra.control.task_state import SubtaskStatus, TaskExecutionState
from orchestra.decomposition.schemas import TaskPlan


class FuturePlanValidationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    errors: list[str] = Field(default_factory=list)


IMMUTABLE_STATUSES = {
    SubtaskStatus.COMMITTED,
    SubtaskStatus.RUNNING,
    SubtaskStatus.AWAITING_CANONICAL_COMMIT,
    SubtaskStatus.RETRY_PENDING,
    SubtaskStatus.FAILED,
    SubtaskStatus.SKIPPED,
    SubtaskStatus.HARNESS_FAILED,
}


class FuturePlanValidator:
    def __init__(self, config: SlowLoopConfig | None = None) -> None:
        self.config = config or SlowLoopConfig()

    def validate(
        self,
        *,
        current_state: TaskExecutionState,
        proposed_plan: TaskPlan,
        edits: list[GlobalEdit],
        leased_subtask_ids: set[str],
    ) -> FuturePlanValidationResult:
        errors: list[str] = []
        current = current_state.task_plan
        current_by_id = {s.subtask_id: s for s in current.subtasks}
        proposed_by_id = {s.subtask_id: s for s in proposed_plan.subtasks}

        if set(current_by_id) != set(proposed_by_id):
            errors.append("M5 forbids adding/removing subtasks")

        for sid, cur in current_by_id.items():
            prop = proposed_by_id.get(sid)
            if prop is None:
                continue
            st = current_state.subtasks[sid]
            frozen = (
                st.status in IMMUTABLE_STATUSES
                or sid in leased_subtask_ids
                or st.lease_status == "leased"
            )
            if frozen:
                if prop.model_dump(mode="json") != cur.model_dump(mode="json"):
                    errors.append(f"immutable subtask modified: {sid}")
                if prop.keystone_harness_id != cur.keystone_harness_id:
                    errors.append(f"keystone harness modified: {sid}")

        if proposed_plan.plan_version <= current.plan_version:
            errors.append("plan_version must increase monotonically")

        try:
            # Delivery targets may be pending/ready/unleased. Only forbid
            # targeting already-finished subtasks.
            validate_communication_plan(
                task_plan=proposed_plan,
                communication_plan=proposed_plan.communication_plan,
                completed_subtask_ids={
                    sid
                    for sid, sub in current_state.subtasks.items()
                    if sub.status
                    in {
                        SubtaskStatus.COMMITTED,
                        SubtaskStatus.FAILED,
                        SubtaskStatus.SKIPPED,
                    }
                },
            )
        except CommunicationPlanValidationError as exc:
            errors.append(str(exc))

        # Backend/model allowlist checks.
        pools = self.config.backend_model_pools
        allowed_backends = {
            b for group in self.config.allowed_backend_assignments.values() for b in group
        }
        for edit in edits:
            if isinstance(edit, PendingBackendAssignmentEdit):
                if allowed_backends and edit.backend_id not in allowed_backends:
                    errors.append(
                        f"backend {edit.backend_id} not in allowlist for {edit.subtask_id}"
                    )
                if edit.model_name and pools:
                    allowed = pools.get(edit.backend_id, [])
                    if allowed and edit.model_name not in allowed:
                        errors.append(
                            f"model {edit.model_name} not in pool for {edit.backend_id}"
                        )
            if isinstance(edit, PendingGraphTemplateEdit):
                if edit.subtask_id in leased_subtask_ids:
                    errors.append(
                        f"graph template edit targets leased subtask {edit.subtask_id}"
                    )

        # Canonical / commit records must be untouched by plan apply (structural).
        _ = communication_plan_hash(proposed_plan.communication_plan)
        return FuturePlanValidationResult(ok=not errors, errors=errors)
