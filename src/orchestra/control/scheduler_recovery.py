"""Scheduler incarnation ownership and stale-lease reclaim.

A normal first scheduler start is **not** a recovery. A new incarnation alone
is not a recovery. A recovery event is persisted only when interrupted state is
reconciled (stale leases reclaimed or incomplete activated work resumed).
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

from orchestra.control.task_state import SubtaskStatus, TaskExecutionState

_TERMINAL = {
    SubtaskStatus.COMMITTED,
    SubtaskStatus.FAILED,
    SubtaskStatus.SKIPPED,
    SubtaskStatus.HARNESS_FAILED,
}


def start_scheduler_incarnation(state: TaskExecutionState) -> int:
    """Bump scheduler incarnation. Does not emit a recovery event."""
    old = int(state.scheduler_incarnation or 0)
    state.scheduler_incarnation = old + 1
    return int(state.scheduler_incarnation)


def reclaim_stale_leases(
    state: TaskExecutionState,
    *,
    reason: str,
    checkpoint_id: str | None = None,
    decision_id: str | None = None,
) -> dict[str, Any] | None:
    """Reclaim noncommitted leases owned by previous inactive incarnations.

    Returns a recovery event dict when at least one lease is reclaimed or an
    incomplete activated decision is being resumed; otherwise ``None``.
    """
    current = int(state.scheduler_incarnation or 0)
    old = max(0, current - 1)
    reclaimed_lease_ids: list[str] = []
    affected_subtasks: list[str] = []
    for sid, sub in sorted(state.subtasks.items()):
        if sub.status in _TERMINAL:
            continue
        if sub.lease_status != "leased":
            continue
        owner = sub.lease_owner_incarnation
        # Only previous inactive incarnations are reclaimable.
        if owner is not None and owner >= current:
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

    pending = getattr(getattr(state, "pareto_state", None), "pending_decision", None)
    incomplete_decision = bool(
        pending is not None
        and getattr(pending, "activated_revision_id", None)
        and getattr(pending, "realization_status", "pending")
        in {"pending", None, "PENDING"}
    )
    if not reclaimed_lease_ids and not incomplete_decision:
        return None

    recovery_id = hashlib.sha256(
        "|".join(
            [
                state.task_id,
                str(old),
                str(current),
                ",".join(reclaimed_lease_ids),
                ",".join(affected_subtasks),
                reason,
            ]
        ).encode()
    ).hexdigest()[:24]
    event = {
        "recovery_id": recovery_id,
        "event": "scheduler_recovery",
        "run_id": state.task_id,
        "old_scheduler_incarnation": old,
        "new_scheduler_incarnation": current,
        "reclaimed_lease_ids": reclaimed_lease_ids,
        "affected_subtask_ids": affected_subtasks,
        "decision_id": decision_id
        or (getattr(pending, "decision_id", None) if pending is not None else None),
        "activation_revision": state.active_plan_revision_id,
        "checkpoint_id": checkpoint_id or state.active_plan_revision_id,
        "reason": reason,
        "persisted_event_time": datetime.now(UTC).isoformat(),
    }
    # Deduplicate by recovery_id across resume/report.
    existing = {
        e.get("recovery_id")
        for e in (state.scheduler_recovery_events or [])
        if isinstance(e, dict)
    }
    if recovery_id not in existing:
        state.scheduler_recovery_events.append(event)
    return event


def begin_scheduler_session(
    state: TaskExecutionState,
    *,
    reason: str = "run_task_start",
    checkpoint_id: str | None = None,
) -> dict[str, Any] | None:
    """Start a new incarnation and reclaim only when interrupted state exists.

    Clean first starts return ``None`` (zero recovery events).
    """
    start_scheduler_incarnation(state)
    # First incarnation with no prior leases/pending → not a recovery.
    if int(state.scheduler_incarnation) == 1 and not any(
        sub.lease_status == "leased" for sub in state.subtasks.values()
    ):
        pending = getattr(getattr(state, "pareto_state", None), "pending_decision", None)
        if pending is None:
            return None
    return reclaim_stale_leases(
        state,
        reason=reason,
        checkpoint_id=checkpoint_id,
    )


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


# Backward-compatible alias used by older call sites / tests.
begin_scheduler_incarnation = begin_scheduler_session
