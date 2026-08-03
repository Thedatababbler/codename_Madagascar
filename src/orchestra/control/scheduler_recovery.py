"""Scheduler incarnation ownership and stale-lease reclaim."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from orchestra.control.task_state import SubtaskStatus, TaskExecutionState

_TERMINAL = {
    SubtaskStatus.COMMITTED,
    SubtaskStatus.FAILED,
    SubtaskStatus.SKIPPED,
    SubtaskStatus.HARNESS_FAILED,
}


def begin_scheduler_incarnation(
    state: TaskExecutionState,
    *,
    reason: str = "run_task_start",
    checkpoint_revision_id: str | None = None,
) -> dict[str, Any]:
    """Start a new scheduler incarnation and reclaim stale leases.

    Reclaims only noncommitted leases owned by a previous inactive incarnation.
    Never reclaims leases owned by the current (live) incarnation.
    """
    old = int(state.scheduler_incarnation or 0)
    new = old + 1
    state.scheduler_incarnation = new
    reclaimed_lease_ids: list[str] = []
    affected_subtasks: list[str] = []
    for sid, sub in sorted(state.subtasks.items()):
        if sub.status in _TERMINAL:
            continue
        if sub.lease_status != "leased":
            continue
        owner = sub.lease_owner_incarnation
        # Missing owner (pre-incarnation checkpoints) or previous incarnation → stale.
        if owner is not None and owner >= new:
            continue
        lease_id = sub.lease_id or f"orphan:{sid}:{owner}"
        reclaimed_lease_ids.append(str(lease_id))
        affected_subtasks.append(sid)
        sub.lease_status = "unleased"
        sub.lease_id = None
        sub.lease_owner_incarnation = None
        sub.lease_created_at = None
        sub.lease_plan_version = None
        sub.lease_acquired_state_version = None
        if sub.status is SubtaskStatus.RUNNING:
            sub.status = SubtaskStatus.READY

    event = {
        "event": "scheduler_incarnation_recovery",
        "old_scheduler_incarnation": old,
        "new_scheduler_incarnation": new,
        "reclaimed_lease_ids": reclaimed_lease_ids,
        "affected_subtasks": affected_subtasks,
        "recovery_reason": reason,
        "checkpoint_revision_id": checkpoint_revision_id or state.active_plan_revision_id,
        "at": datetime.now(UTC).isoformat(),
    }
    state.scheduler_recovery_events.append(event)
    return event


def acquire_lease(
    state: TaskExecutionState,
    subtask_id: str,
) -> str:
    """Stamp lease ownership for the current scheduler incarnation."""
    sub = state.subtasks[subtask_id]
    lease_id = (
        f"{state.task_id}:{subtask_id}:inc{state.scheduler_incarnation}:"
        f"{state.state_version}"
    )
    sub.lease_status = "leased"
    sub.lease_id = lease_id
    sub.lease_owner_incarnation = int(state.scheduler_incarnation)
    sub.lease_created_at = datetime.now(UTC)
    sub.lease_plan_version = state.task_plan.plan_version
    sub.lease_acquired_state_version = state.state_version
    return lease_id
