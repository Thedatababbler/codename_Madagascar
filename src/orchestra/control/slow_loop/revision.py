"""Atomic plan revision preparation and coordinator-owned commit."""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from orchestra.communication.compiler import communication_plan_hash
from orchestra.communication.plan import CommunicationPlan
from orchestra.control.slow_loop.graph_materializer import FutureGraphMaterializer
from orchestra.control.slow_loop.schemas import (
    FutureGraphRevision,
    GlobalDiagnosis,
    GlobalEdit,
    GlobalPlanRevision,
    GlobalPlanRevisionStatus,
    SlowLoopTriggerReason,
    TaskSchedulingPolicy,
)
from orchestra.control.task_state import SubtaskStatus, TaskExecutionState
from orchestra.decomposition.schemas import TaskPlan
from orchestra.runtime.task_checkpoint import TaskCheckpointStore


class PreparedSlowLoopRevision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: GlobalPlanRevision
    proposed_task_plan: TaskPlan
    proposed_communication_plan: CommunicationPlan
    proposed_scheduling_policy: TaskSchedulingPolicy
    future_graph_revisions: list[FutureGraphRevision] = Field(default_factory=list)
    projected_state: TaskExecutionState
    staging_directory: str


class PlanRevisionCorruption(RuntimeError):
    pass


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


def prepare_revision_staging(
    *,
    run_dir: str | Path,
    revision: GlobalPlanRevision,
    new_plan: TaskPlan,
    new_communication: CommunicationPlan,
    scheduling_policy: TaskSchedulingPolicy,
    state: TaskExecutionState,
    allowed_backend_pools: dict[str, list[str]] | None = None,
    backend_model_pools: dict[str, list[str]] | None = None,
) -> PreparedSlowLoopRevision:
    """Write VALIDATED revision files to a staging directory (not active yet)."""
    root = Path(run_dir) / "plan_revisions"
    root.mkdir(parents=True, exist_ok=True)
    staging = root / f".staging-{revision.revision_id}-{os.getpid()}"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    rev = revision.model_copy(deep=True)
    rev.status = GlobalPlanRevisionStatus.VALIDATED

    (staging / "task_plan.json").write_text(
        new_plan.model_dump_json(indent=2), encoding="utf-8"
    )
    (staging / "communication_plan.json").write_text(
        new_communication.model_dump_json(indent=2), encoding="utf-8"
    )
    (staging / "scheduling_policy.json").write_text(
        scheduling_policy.model_dump_json(indent=2), encoding="utf-8"
    )
    (staging / "revision.json").write_text(
        rev.model_dump_json(indent=2), encoding="utf-8"
    )

    graphs_dir = staging / "graphs"
    graphs_dir.mkdir()
    materializer = FutureGraphMaterializer()
    future_graph_revisions: list[FutureGraphRevision] = []
    projected = state.model_copy(deep=True)
    eligible = set(rev.eligible_subtask_ids)
    by_spec = {s.subtask_id: s for s in new_plan.subtasks}

    for sid in sorted(eligible):
        sub = projected.subtasks.get(sid)
        if sub is None:
            continue
        if sub.status not in {SubtaskStatus.PENDING, SubtaskStatus.READY}:
            continue
        if sub.lease_status == "leased":
            continue
        spec = by_spec[sid]
        needs_materialize = bool(
            spec.metadata.get("backend_assignment")
            or spec.metadata.get("model_assignment")
            or any(
                getattr(e, "subtask_id", None) == sid
                and e.type
                in {
                    "pending_backend_assignment",
                    "pending_graph_template",
                }
                for e in rev.edits
            )
        )
        if not needs_materialize:
            marker = graphs_dir / f"{sid}.txt"
            marker.write_text(spec.local_graph_template + "\n", encoding="utf-8")
            future_graph_revisions.append(
                FutureGraphRevision(
                    subtask_id=sid,
                    parent_graph_hash="",
                    graph_hash="",
                    graph_path=spec.local_graph_template,
                    edits=[],
                )
            )
            continue
        mat = materializer.materialize(
            subtask=spec,
            active_plan_revision=rev,
            allowed_backend_pools=allowed_backend_pools,
            backend_model_pools=backend_model_pools,
            revision_graphs_dir=graphs_dir,
            revision_id=rev.revision_id,
        )
        new_meta = dict(spec.metadata)
        new_meta["execution_config"] = mat.execution_config.model_dump(mode="json")
        new_meta["materialized_graph_path"] = mat.graph_path
        updated_spec = spec.model_copy(
            update={
                "local_graph_template": mat.graph_path,
                "metadata": new_meta,
            }
        )
        for i, s in enumerate(new_plan.subtasks):
            if s.subtask_id == sid:
                subs = list(new_plan.subtasks)
                subs[i] = updated_spec
                new_plan = new_plan.model_copy(update={"subtasks": subs})
                by_spec[sid] = updated_spec
                break
        sub.spec = updated_spec
        future_graph_revisions.append(
            FutureGraphRevision(
                subtask_id=sid,
                parent_graph_hash=mat.parent_graph_hash,
                graph_hash=mat.graph_hash,
                graph_path=mat.graph_path,
                edits=[
                    e
                    for e in rev.edits
                    if getattr(e, "subtask_id", None) == sid
                ],
            )
        )

    # Rewrite task plan after materialization path updates.
    (staging / "task_plan.json").write_text(
        new_plan.model_dump_json(indent=2), encoding="utf-8"
    )
    rev = rev.model_copy(update={"new_plan_hash": new_plan.content_hash()})
    (staging / "revision.json").write_text(
        rev.model_dump_json(indent=2), encoding="utf-8"
    )

    for path in staging.rglob("*"):
        if path.is_file():
            with path.open("a", encoding="utf-8") as handle:
                handle.flush()
                os.fsync(handle.fileno())

    projected.task_plan = new_plan
    projected.communication_plan = new_communication.model_copy(deep=True)
    projected.plan_content_hash = new_plan.content_hash()
    projected.scheduling_policy = scheduling_policy
    projected.active_plan_revision_id = rev.revision_id
    # Do not mark APPLIED yet — commit step does.
    return PreparedSlowLoopRevision(
        revision=rev,
        proposed_task_plan=new_plan,
        proposed_communication_plan=new_communication,
        proposed_scheduling_policy=scheduling_policy,
        future_graph_revisions=future_graph_revisions,
        projected_state=projected,
        staging_directory=str(staging),
    )


def write_plan_revision_snapshot(
    *,
    run_dir: str | Path,
    revision: GlobalPlanRevision,
    new_plan: TaskPlan,
) -> Path:
    """Legacy helper: prepare + promote staging immediately (tests/smoke)."""
    policy = TaskSchedulingPolicy()
    prepared = prepare_revision_staging(
        run_dir=run_dir,
        revision=revision,
        new_plan=new_plan,
        new_communication=new_plan.communication_plan,
        scheduling_policy=policy,
        state=TaskExecutionState.from_plan(new_plan),
    )
    final = Path(run_dir) / "plan_revisions" / revision.revision_id
    if final.exists():
        shutil.rmtree(final)
    os.replace(prepared.staging_directory, final)
    # Mark applied on disk snapshot for legacy callers.
    rev = revision.model_copy(deep=True)
    rev.status = GlobalPlanRevisionStatus.APPLIED
    (final / "revision.json").write_text(
        rev.model_dump_json(indent=2), encoding="utf-8"
    )
    return final


def apply_revision_to_state(
    *,
    state: TaskExecutionState,
    revision: GlobalPlanRevision,
    new_plan: TaskPlan,
    scheduling_policy,
) -> TaskExecutionState:
    """Mutate state in-place (used after transactional commit)."""
    state.task_plan = new_plan
    state.communication_plan = new_plan.communication_plan.model_copy(deep=True)
    state.plan_content_hash = new_plan.content_hash()
    state.scheduling_policy = scheduling_policy
    state.active_plan_hash = new_plan.content_hash()
    state.active_communication_hash = communication_plan_hash(state.communication_plan)
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


async def commit_prepared_revision(
    *,
    state: TaskExecutionState,
    prepared: PreparedSlowLoopRevision,
    checkpoint_store: TaskCheckpointStore,
    run_dir: str | Path,
) -> TaskExecutionState:
    """
    Coordinator-owned atomic commit:

    staging revision files → staged checkpoint → atomic rename revision
    → atomic replace checkpoint → swap in-memory state.
    """
    staging = Path(prepared.staging_directory)
    final = Path(run_dir) / "plan_revisions" / prepared.revision.revision_id
    if not staging.exists():
        raise PlanRevisionCorruption(
            f"PLAN_REVISION_CORRUPTION: missing staging {staging}"
        )

    # Projected state already holds new plan; mark revision APPLIED for checkpoint.
    projected = prepared.projected_state.model_copy(deep=True)
    rev = prepared.revision.model_copy(deep=True)
    rev.status = GlobalPlanRevisionStatus.APPLIED
    rev.applied_at_state_version = state.state_version + 1
    for old in projected.plan_revision_history:
        if (
            getattr(old, "status", None) is GlobalPlanRevisionStatus.APPLIED
            and old.revision_id != rev.revision_id
        ):
            old.status = GlobalPlanRevisionStatus.SUPERSEDED
    # Avoid duplicate append if prepare already mirrored.
    projected.plan_revision_history = [
        r
        for r in projected.plan_revision_history
        if getattr(r, "revision_id", None) != rev.revision_id
    ]
    projected.plan_revision_history.append(rev)
    projected.active_plan_revision_id = rev.revision_id
    projected.active_plan_hash = prepared.proposed_task_plan.content_hash()
    projected.active_communication_hash = communication_plan_hash(
        prepared.proposed_communication_plan
    )
    projected.global_revision = state.global_revision + 1
    projected.state_version = state.state_version + 1
    # Preserve live delivery ledger / committed history from current state.
    projected.delivery_ledger = list(state.delivery_ledger)
    projected.workspace_commit_records = list(state.workspace_commit_records)
    projected.fast_loop_history = list(state.fast_loop_history)
    projected.slow_loop_history = list(state.slow_loop_history)
    projected.fast_loop_states = dict(state.fast_loop_states)
    projected.canonical_workspace_ref = state.canonical_workspace_ref
    projected.canonical_revision = state.canonical_revision
    projected.committed_subtask_count = state.committed_subtask_count
    # Frozen statuses must keep their specs from live state.
    for sid, live_sub in state.subtasks.items():
        if live_sub.status not in {SubtaskStatus.PENDING, SubtaskStatus.READY}:
            projected.subtasks[sid] = live_sub.model_copy(deep=True)
        elif live_sub.lease_status == "leased":
            projected.subtasks[sid] = live_sub.model_copy(deep=True)

    # Write APPLIED revision.json into staging before promote.
    (staging / "revision.json").write_text(
        rev.model_dump_json(indent=2), encoding="utf-8"
    )
    with (staging / "revision.json").open("a", encoding="utf-8") as handle:
        handle.flush()
        os.fsync(handle.fileno())

    # Save checkpoint for projected state first (tmp→replace inside store).
    # If this fails, staging remains non-active and live state untouched.
    await checkpoint_store.save(projected)

    # Atomic promote of revision directory.
    if final.exists():
        shutil.rmtree(final)
    os.replace(staging, final)

    # Verify revision/checkpoint agreement.
    if projected.active_plan_revision_id != rev.revision_id:
        raise PlanRevisionCorruption("PLAN_REVISION_CORRUPTION: revision id mismatch")
    disk_rev = GlobalPlanRevision.model_validate_json(
        (final / "revision.json").read_text(encoding="utf-8")
    )
    if disk_rev.status is not GlobalPlanRevisionStatus.APPLIED:
        raise PlanRevisionCorruption(
            "PLAN_REVISION_CORRUPTION: disk revision not APPLIED"
        )

    # Swap in-memory fields from projected onto live state object.
    state.task_plan = projected.task_plan
    state.communication_plan = projected.communication_plan
    state.plan_content_hash = projected.plan_content_hash
    state.scheduling_policy = projected.scheduling_policy
    state.active_plan_revision_id = projected.active_plan_revision_id
    state.active_plan_hash = projected.active_plan_hash
    state.active_communication_hash = projected.active_communication_hash
    state.plan_revision_history = projected.plan_revision_history
    state.global_revision = projected.global_revision
    state.state_version = projected.state_version
    state.slow_loop_state = projected.slow_loop_state
    for sid, sub in projected.subtasks.items():
        if state.subtasks[sid].lease_status == "leased":
            continue
        if state.subtasks[sid].status in {
            SubtaskStatus.PENDING,
            SubtaskStatus.READY,
        }:
            state.subtasks[sid].spec = sub.spec
    return state


def verify_checkpoint_revision_consistency(
    *,
    state: TaskExecutionState,
    run_dir: str | Path,
) -> None:
    if not state.active_plan_revision_id:
        return
    rev_path = (
        Path(run_dir) / "plan_revisions" / state.active_plan_revision_id / "revision.json"
    )
    if not rev_path.exists():
        raise PlanRevisionCorruption(
            "PLAN_REVISION_CORRUPTION: active revision directory missing"
        )
    disk = GlobalPlanRevision.model_validate_json(rev_path.read_text(encoding="utf-8"))
    if disk.status is not GlobalPlanRevisionStatus.APPLIED:
        raise PlanRevisionCorruption(
            "PLAN_REVISION_CORRUPTION: active revision not APPLIED on disk"
        )
    if state.active_plan_hash and disk.new_plan_hash != state.active_plan_hash:
        raise PlanRevisionCorruption(
            "PLAN_REVISION_CORRUPTION: plan hash mismatch vs revision"
        )
    if (
        state.active_communication_hash
        and disk.new_communication_hash != state.active_communication_hash
    ):
        raise PlanRevisionCorruption(
            "PLAN_REVISION_CORRUPTION: communication hash mismatch vs revision"
        )
