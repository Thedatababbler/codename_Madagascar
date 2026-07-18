"""Future-only plan validation for Slow Loop revisions."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from orchestra.communication.compiler import communication_plan_hash
from orchestra.communication.validation import (
    CommunicationPlanValidationError,
    validate_communication_plan,
)
from orchestra.control.slow_loop.edits import apply_global_edits
from orchestra.control.slow_loop.schemas import (
    GlobalEdit,
    PendingBackendAssignmentEdit,
    PendingGraphTemplateEdit,
    SlowLoopConfig,
    TaskSchedulingPolicy,
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

FORBIDDEN_PENDING_FIELDS = (
    "objective",
    "dependencies",
    "subtask_id",
    "keystone_harness_id",
)


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
        proposed_scheduling_policy: TaskSchedulingPolicy | None = None,
        proposed_communication_plan=None,
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
            else:
                for field in FORBIDDEN_PENDING_FIELDS:
                    if getattr(prop, field) != getattr(cur, field):
                        errors.append(
                            f"UNDECLARED_PLAN_MUTATION: forbidden field "
                            f"{field} changed on {sid}"
                        )

        if proposed_plan.plan_version <= current.plan_version:
            errors.append("plan_version must increase monotonically")

        policy = proposed_scheduling_policy
        if policy is None:
            policy = current_state.scheduling_policy or TaskSchedulingPolicy()
            if not isinstance(policy, TaskSchedulingPolicy):
                policy = TaskSchedulingPolicy.model_validate(policy)

        # Canonical edit application — proposed state must match independently
        # derived state from declared edits.
        eligible = {
            sid
            for sid, st in current_state.subtasks.items()
            if st.status in {SubtaskStatus.PENDING, SubtaskStatus.READY}
            and sid not in leased_subtask_ids
            and st.lease_status != "leased"
        }
        base_policy = current_state.scheduling_policy or TaskSchedulingPolicy()
        if not isinstance(base_policy, TaskSchedulingPolicy):
            base_policy = TaskSchedulingPolicy.model_validate(base_policy)
        derived_plan, derived_comm, derived_policy, _rejected = apply_global_edits(
            task_plan=current,
            communication_plan=current_state.communication_plan,
            scheduling_policy=base_policy,
            edits=edits,
            eligible_subtask_ids=eligible,
        )
        del policy

        # Compare structural hashes / dumps for undeclared mutations.
        if derived_plan.content_hash() != proposed_plan.content_hash():
            # Allow communication_plan nested in task plan to be the proposed one
            # if candidate attached it; compare subtasks + version carefully.
            derived_subs = [s.model_dump(mode="json") for s in derived_plan.subtasks]
            proposed_subs = [s.model_dump(mode="json") for s in proposed_plan.subtasks]
            if derived_subs != proposed_subs:
                errors.append(
                    "UNDECLARED_PLAN_MUTATION: proposed subtasks differ from "
                    "apply_global_edits(edits)"
                )
            if derived_plan.plan_version != proposed_plan.plan_version:
                errors.append(
                    "UNDECLARED_PLAN_MUTATION: plan_version mismatch vs derived"
                )

        derived_comm_hash = communication_plan_hash(derived_comm)
        proposed_comm = proposed_communication_plan or proposed_plan.communication_plan
        if derived_comm_hash != communication_plan_hash(proposed_comm):
            errors.append(
                "UNDECLARED_PLAN_MUTATION: proposed communication_plan differs "
                "from apply_global_edits(edits)"
            )

        if proposed_scheduling_policy is not None:
            if (
                derived_policy.model_dump(mode="json")
                != proposed_scheduling_policy.model_dump(mode="json")
            ):
                errors.append(
                    "UNDECLARED_PLAN_MUTATION: proposed scheduling_policy differs "
                    "from apply_global_edits(edits)"
                )

        try:
            validate_communication_plan(
                task_plan=proposed_plan,
                communication_plan=proposed_comm,
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

        return FuturePlanValidationResult(ok=not errors, errors=errors)
