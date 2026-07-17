"""Atomic plan revision preparation and apply helpers."""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

from orchestra.communication.compiler import communication_plan_hash
from orchestra.control.slow_loop.schemas import (
    GlobalDiagnosis,
    GlobalEdit,
    GlobalPlanRevision,
    GlobalPlanRevisionStatus,
    SlowLoopTriggerReason,
)
from orchestra.control.task_state import SubtaskStatus, TaskExecutionState
from orchestra.decomposition.schemas import TaskPlan


def build_revision(
    *,
    state: TaskExecutionState,
    parent_plan: TaskPlan,
    new_plan: TaskPlan,
    edits: list[GlobalEdit],
    diagnosis: GlobalDiagnosis,
    triggers: list[SlowLoopTriggerReason],
    eligible: list[str],
    rejected_edit_ids: list[str],
) -> GlobalPlanRevision:
    parent_comm_hash = communication_plan_hash(state.communication_plan)
    new_comm_hash = communication_plan_hash(new_plan.communication_plan)
    rev_num = len(state.plan_revision_history) + 1
    return GlobalPlanRevision(
        revision_id=f"rev-{rev_num}-{uuid.uuid4().hex[:8]}",
        revision_number=rev_num,
        parent_revision_id=state.active_plan_revision_id,
        parent_plan_hash=parent_plan.content_hash(),
        new_plan_hash=new_plan.content_hash(),
        parent_communication_hash=parent_comm_hash,
        new_communication_hash=new_comm_hash,
        trigger_reasons=triggers,
        diagnosis=diagnosis,
        edits=edits,
        eligible_subtask_ids=sorted(eligible),
        rejected_edit_ids=rejected_edit_ids,
        created_at_state_version=state.state_version,
        status=GlobalPlanRevisionStatus.PROPOSED,
    )


def write_plan_revision_snapshot(
    *,
    run_dir: str | Path,
    revision: GlobalPlanRevision,
    new_plan: TaskPlan,
) -> Path:
    """Write immutable revision files under plan_revisions/<id>/ (atomic rename)."""
    root = Path(run_dir) / "plan_revisions"
    root.mkdir(parents=True, exist_ok=True)
    tmp = root / f".tmp-{revision.revision_id}-{os.getpid()}"
    final = root / revision.revision_id
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    (tmp / "task_plan.json").write_text(
        new_plan.model_dump_json(indent=2), encoding="utf-8"
    )
    (tmp / "revision.json").write_text(
        revision.model_dump_json(indent=2), encoding="utf-8"
    )
    graphs = tmp / "graphs"
    graphs.mkdir()
    for spec in new_plan.subtasks:
        # Record assignment path only; do not copy/overwrite original YAML.
        marker = graphs / f"{spec.subtask_id}.txt"
        marker.write_text(spec.local_graph_template + "\n", encoding="utf-8")
    # fsync directory entries best-effort
    for path in tmp.rglob("*"):
        if path.is_file():
            with path.open("a", encoding="utf-8") as handle:
                handle.flush()
                os.fsync(handle.fileno())
    if final.exists():
        shutil.rmtree(final)
    os.replace(tmp, final)
    return final


def apply_revision_to_state(
    *,
    state: TaskExecutionState,
    revision: GlobalPlanRevision,
    new_plan: TaskPlan,
    scheduling_policy,
) -> TaskExecutionState:
    """Mutate state in-place under coordinator lock (caller holds lock)."""
    state.task_plan = new_plan
    state.communication_plan = new_plan.communication_plan.model_copy(deep=True)
    state.plan_content_hash = new_plan.content_hash()
    state.scheduling_policy = scheduling_policy
    # Update only eligible pending/ready-unleased specs.
    eligible = set(revision.eligible_subtask_ids)
    by_spec = {s.subtask_id: s for s in new_plan.subtasks}
    for sid in eligible:
        sub = state.subtasks.get(sid)
        if sub is None:
            continue
        if sub.status not in {SubtaskStatus.PENDING, SubtaskStatus.READY}:
            continue
        if sub.lease_status != "unleased":
            continue
        sub.spec = by_spec[sid]
    revision.status = GlobalPlanRevisionStatus.APPLIED
    revision.applied_at_state_version = state.state_version + 1
    # Supersede prior applied
    for old in state.plan_revision_history:
        if (
            hasattr(old, "status")
            and old.status is GlobalPlanRevisionStatus.APPLIED
            and old.revision_id != revision.revision_id
        ):
            old.status = GlobalPlanRevisionStatus.SUPERSEDED
    state.plan_revision_history.append(revision)
    state.active_plan_revision_id = revision.revision_id
    state.global_revision += 1
    state.state_version += 1
    return state
