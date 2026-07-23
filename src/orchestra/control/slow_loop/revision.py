"""Atomic plan revision preparation and coordinator-owned commit."""

from __future__ import annotations

import os
import shutil
import uuid
from collections.abc import Callable
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
from orchestra.control.task_state import (
    GlobalUpdateRecord,
    SubtaskStatus,
    TaskExecutionState,
)
from orchestra.decomposition.schemas import TaskPlan
from orchestra.ir.graph import load_graph
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
    final_directory: str = ""


class PlanRevisionCorruption(RuntimeError):
    pass


class PlanRevisionIdCollision(PlanRevisionCorruption):
    pass


class RevisionAlreadyActivated(RuntimeError):
    """Checkpoint activation succeeded; live state may still be stale.

    Callers must not report keep_previous_plan. Apply ``projected_state`` (or
    reload the checkpoint) onto the live TaskExecutionState.
    """

    def __init__(
        self,
        *,
        revision_id: str,
        projected_state: TaskExecutionState,
        message: str = "",
    ) -> None:
        self.revision_id = revision_id
        self.projected_state = projected_state
        super().__init__(
            message
            or f"REVISION_ALREADY_ACTIVATED: {revision_id} committed in checkpoint"
        )


class RevisionTransactionHooks(BaseModel):
    """Test-only crash injection points. Production leaves all None."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    after_staging_fsync: Callable[[], None] | None = None
    after_revision_promote: Callable[[], None] | None = None
    before_checkpoint_save: Callable[[], None] | None = None
    after_checkpoint_save: Callable[[], None] | None = None


def merge_history_by_record_id(
    *histories: list,
) -> list[GlobalUpdateRecord]:
    """Merge GlobalUpdateRecord lists by stable record_id (retry/idempotency)."""
    merged: dict[str, GlobalUpdateRecord] = {}
    order: list[str] = []
    for history in histories:
        for raw in history or []:
            rec = (
                raw
                if isinstance(raw, GlobalUpdateRecord)
                else GlobalUpdateRecord.model_validate(raw)
            )
            if rec.record_id not in merged:
                order.append(rec.record_id)
            merged[rec.record_id] = rec
    return [merged[rid] for rid in order]


def apply_projected_state_to_live(
    state: TaskExecutionState,
    projected: TaskExecutionState,
) -> TaskExecutionState:
    """Copy activated projected fields onto the live state object."""
    state.task_plan = projected.task_plan
    state.communication_plan = projected.communication_plan
    state.plan_content_hash = projected.plan_content_hash
    state.scheduling_policy = projected.scheduling_policy
    state.active_plan_revision_id = projected.active_plan_revision_id
    state.active_plan_hash = projected.active_plan_hash
    state.active_communication_hash = projected.active_communication_hash
    state.plan_revision_history = list(projected.plan_revision_history)
    state.slow_loop_history = merge_history_by_record_id(
        list(state.slow_loop_history or []),
        list(projected.slow_loop_history or []),
    )
    state.global_revision = projected.global_revision
    state.state_version = projected.state_version
    state.slow_loop_state = projected.slow_loop_state
    state.pareto_state = projected.pareto_state
    for sid, sub in projected.subtasks.items():
        if sid not in state.subtasks:
            state.subtasks[sid] = sub.model_copy(deep=True)
            continue
        if state.subtasks[sid].lease_status == "leased":
            continue
        if state.subtasks[sid].status in {
            SubtaskStatus.PENDING,
            SubtaskStatus.READY,
        }:
            state.subtasks[sid].spec = sub.spec
    return state


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


def _fsync_tree(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_file():
            with path.open("a", encoding="utf-8") as handle:
                handle.flush()
                os.fsync(handle.fileno())
    try:
        dir_fd = os.open(str(root), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass


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
    """Write PREPARED revision files to staging; plan stores final graph paths."""
    root = Path(run_dir) / "plan_revisions"
    root.mkdir(parents=True, exist_ok=True)
    staging = root / f".staging-{revision.revision_id}-{os.getpid()}"
    final = root / revision.revision_id
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    rev = revision.model_copy(deep=True)
    rev.status = GlobalPlanRevisionStatus.PREPARED
    materialized_plan = new_plan.model_copy(deep=True)

    (staging / "communication_plan.json").write_text(
        new_communication.model_dump_json(indent=2), encoding="utf-8"
    )
    (staging / "scheduling_policy.json").write_text(
        scheduling_policy.model_dump_json(indent=2), encoding="utf-8"
    )

    graphs_staging = staging / "graphs"
    graphs_staging.mkdir()
    graphs_final = final / "graphs"
    materializer = FutureGraphMaterializer()
    future_graph_revisions: list[FutureGraphRevision] = []
    projected = state.model_copy(deep=True)
    eligible = set(rev.eligible_subtask_ids)
    by_spec = {s.subtask_id: s for s in materialized_plan.subtasks}

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
            marker = graphs_staging / f"{sid}.txt"
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
            staging_graphs_dir=graphs_staging,
            final_graphs_dir=graphs_final,
            revision_id=rev.revision_id,
        )
        logical_path = mat.graph_path
        if ".staging-" in logical_path.replace("\\", "/"):
            raise PlanRevisionCorruption(
                "PLAN_REVISION_GRAPH_PATH_INVALID: stored path is staging"
            )
        new_meta = dict(spec.metadata)
        new_meta["execution_config"] = mat.execution_config.model_dump(mode="json")
        new_meta["materialized_graph_path"] = logical_path
        updated_spec = spec.model_copy(
            update={
                "local_graph_template": logical_path,
                "metadata": new_meta,
            }
        )
        for i, s in enumerate(materialized_plan.subtasks):
            if s.subtask_id == sid:
                subs = list(materialized_plan.subtasks)
                subs[i] = updated_spec
                materialized_plan = materialized_plan.model_copy(
                    update={"subtasks": subs}
                )
                by_spec[sid] = updated_spec
                break
        sub.spec = updated_spec
        future_graph_revisions.append(
            FutureGraphRevision(
                subtask_id=sid,
                parent_graph_hash=mat.parent_graph_hash,
                graph_hash=mat.graph_hash,
                graph_path=logical_path,
                edits=[
                    e
                    for e in rev.edits
                    if getattr(e, "subtask_id", None) == sid
                ],
            )
        )

    rev = rev.model_copy(
        update={
            "new_plan_hash": materialized_plan.content_hash(),
            "new_communication_hash": communication_plan_hash(new_communication),
        }
    )
    (staging / "task_plan.json").write_text(
        materialized_plan.model_dump_json(indent=2), encoding="utf-8"
    )
    (staging / "revision.json").write_text(
        rev.model_dump_json(indent=2), encoding="utf-8"
    )
    _fsync_tree(staging)

    projected.task_plan = materialized_plan
    projected.communication_plan = new_communication.model_copy(deep=True)
    projected.plan_content_hash = materialized_plan.content_hash()
    projected.scheduling_policy = scheduling_policy
    projected.active_plan_revision_id = rev.revision_id
    return PreparedSlowLoopRevision(
        revision=rev,
        proposed_task_plan=materialized_plan,
        proposed_communication_plan=new_communication,
        proposed_scheduling_policy=scheduling_policy,
        future_graph_revisions=future_graph_revisions,
        projected_state=projected,
        staging_directory=str(staging),
        final_directory=str(final),
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
    final = Path(prepared.final_directory)
    staging = Path(prepared.staging_directory)
    if final.exists():
        disk = GlobalPlanRevision.model_validate_json(
            (final / "revision.json").read_text(encoding="utf-8")
        )
        if disk.new_plan_hash == prepared.revision.new_plan_hash:
            shutil.rmtree(staging, ignore_errors=True)
            return final
        raise PlanRevisionIdCollision(
            "PLAN_REVISION_ID_COLLISION: final revision exists with different hash"
        )
    os.replace(staging, final)
    rev = prepared.revision.model_copy(
        update={"status": GlobalPlanRevisionStatus.APPLIED}
    )
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


def _promote_staging(
    *,
    staging: Path,
    final: Path,
    prepared: PreparedSlowLoopRevision,
) -> GlobalPlanRevision:
    """Promote staging → final. Active pointer remains old checkpoint until save."""
    if not staging.exists():
        raise PlanRevisionCorruption(
            f"PLAN_REVISION_CORRUPTION: missing staging {staging}"
        )
    rev = prepared.revision.model_copy(deep=True)
    rev.status = GlobalPlanRevisionStatus.PROMOTED
    (staging / "revision.json").write_text(
        rev.model_dump_json(indent=2), encoding="utf-8"
    )
    _fsync_tree(staging)

    if final.exists():
        disk = GlobalPlanRevision.model_validate_json(
            (final / "revision.json").read_text(encoding="utf-8")
        )
        if disk.new_plan_hash == rev.new_plan_hash:
            # Idempotent recovery: reuse existing final.
            shutil.rmtree(staging, ignore_errors=True)
            return disk
        raise PlanRevisionIdCollision(
            "PLAN_REVISION_ID_COLLISION: final revision exists with different hash"
        )

    parent = final.parent
    os.replace(staging, final)
    try:
        dir_fd = os.open(str(parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass
    return rev


async def commit_prepared_revision(
    *,
    state: TaskExecutionState,
    prepared: PreparedSlowLoopRevision,
    checkpoint_store: TaskCheckpointStore,
    run_dir: str | Path,
    hooks: RevisionTransactionHooks | None = None,
) -> TaskExecutionState:
    """
    Crash-safe commit order:

    1. staging fsync (done in prepare)
    2. atomic rename staging → final revision
    3. fsync plan_revisions parent
    4. save projected checkpoint (activates pointer)
    5. swap live in-memory state

    Active checkpoint never points at a missing revision directory.
    """
    hooks = hooks or RevisionTransactionHooks()
    staging = Path(prepared.staging_directory)
    final = Path(prepared.final_directory or (
        Path(run_dir) / "plan_revisions" / prepared.revision.revision_id
    ))

    if hooks.after_staging_fsync is not None:
        hooks.after_staging_fsync()

    promoted_rev = _promote_staging(
        staging=staging, final=final, prepared=prepared
    )

    if hooks.after_revision_promote is not None:
        hooks.after_revision_promote()

    # Build projected state that will become active only after checkpoint save.
    projected = prepared.projected_state.model_copy(deep=True)
    rev = promoted_rev.model_copy(deep=True)
    rev.status = GlobalPlanRevisionStatus.APPLIED
    rev.applied_at_state_version = state.state_version + 1
    for old in projected.plan_revision_history:
        if (
            getattr(old, "status", None) is GlobalPlanRevisionStatus.APPLIED
            and old.revision_id != rev.revision_id
        ):
            old.status = GlobalPlanRevisionStatus.SUPERSEDED
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
    projected.delivery_ledger = list(state.delivery_ledger)
    projected.workspace_commit_records = list(state.workspace_commit_records)
    projected.fast_loop_history = list(state.fast_loop_history)
    # Preserve GlobalUpdateRecord stamped onto prepared.projected_state; merge by
    # record_id so retry/idempotent activation cannot duplicate history rows.
    projected.slow_loop_history = merge_history_by_record_id(
        list(state.slow_loop_history or []),
        list(prepared.projected_state.slow_loop_history or []),
    )
    projected.fast_loop_states = dict(state.fast_loop_states)
    projected.canonical_workspace_ref = state.canonical_workspace_ref
    projected.canonical_revision = state.canonical_revision
    projected.committed_subtask_count = state.committed_subtask_count
    for sid, live_sub in state.subtasks.items():
        if live_sub.status not in {SubtaskStatus.PENDING, SubtaskStatus.READY}:
            projected.subtasks[sid] = live_sub.model_copy(deep=True)
        elif live_sub.lease_status == "leased":
            projected.subtasks[sid] = live_sub.model_copy(deep=True)

    # Persist APPLIED status on promoted revision before activating checkpoint.
    (final / "revision.json").write_text(
        rev.model_dump_json(indent=2), encoding="utf-8"
    )
    with (final / "revision.json").open("a", encoding="utf-8") as handle:
        handle.flush()
        os.fsync(handle.fileno())

    # Pre-activation consistency: final revision must exist with expected hash.
    if not final.exists() or not (final / "revision.json").exists():
        raise PlanRevisionCorruption(
            "PLAN_REVISION_CORRUPTION: final revision missing before checkpoint"
        )
    disk_pre = GlobalPlanRevision.model_validate_json(
        (final / "revision.json").read_text(encoding="utf-8")
    )
    if disk_pre.new_plan_hash != rev.new_plan_hash:
        raise PlanRevisionCorruption(
            "PLAN_REVISION_CORRUPTION: pre-activation plan hash mismatch"
        )
    if disk_pre.status is not GlobalPlanRevisionStatus.APPLIED:
        raise PlanRevisionCorruption(
            "PLAN_REVISION_CORRUPTION: revision not APPLIED before checkpoint"
        )

    if hooks.before_checkpoint_save is not None:
        hooks.before_checkpoint_save()

    # Checkpoint activation is the sole active pointer.
    await checkpoint_store.save(projected)

    try:
        if hooks.after_checkpoint_save is not None:
            hooks.after_checkpoint_save()
        verify_checkpoint_revision_consistency(state=projected, run_dir=run_dir)
        apply_projected_state_to_live(state, projected)
    except RevisionAlreadyActivated:
        raise
    except Exception as exc:  # noqa: BLE001
        # Post-activation: never claim keep_previous_plan / stale live state.
        raise RevisionAlreadyActivated(
            revision_id=rev.revision_id,
            projected_state=projected,
            message=str(exc),
        ) from exc
    return state


def verify_checkpoint_revision_consistency(
    *,
    state: TaskExecutionState,
    run_dir: str | Path,
) -> None:
    """Active pointer is the sole source of truth for applied revisions."""
    if not state.active_plan_revision_id:
        return
    rev_dir = Path(run_dir) / "plan_revisions" / state.active_plan_revision_id
    rev_path = rev_dir / "revision.json"
    if not rev_path.exists():
        raise PlanRevisionCorruption(
            "PLAN_REVISION_CORRUPTION: active revision directory missing"
        )
    disk = GlobalPlanRevision.model_validate_json(rev_path.read_text(encoding="utf-8"))
    # Active is defined by checkpoint pointer; disk status should be APPLIED
    # after a successful commit (PROMOTED is only pre-activation).
    if disk.status not in {
        GlobalPlanRevisionStatus.APPLIED,
        GlobalPlanRevisionStatus.PROMOTED,
    }:
        raise PlanRevisionCorruption(
            f"PLAN_REVISION_CORRUPTION: unexpected revision status {disk.status}"
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

    for spec in state.task_plan.subtasks:
        meta = dict(spec.metadata or {})
        exec_cfg = meta.get("execution_config") or {}
        graph_path = str(
            exec_cfg.get("graph_path")
            or meta.get("materialized_graph_path")
            or ""
        )
        if not graph_path:
            continue
        if ".staging-" in graph_path.replace("\\", "/"):
            raise PlanRevisionCorruption(
                "PLAN_REVISION_GRAPH_PATH_INVALID: staging path in active plan"
            )
        path = Path(graph_path)
        if not path.is_absolute():
            # Paths may be relative to run_dir or cwd; try both.
            candidates = [path, Path(run_dir) / path, Path.cwd() / path]
        else:
            candidates = [path]
        existing = next((p for p in candidates if p.exists()), None)
        if existing is None:
            # Only required when this is a materialized revision snapshot.
            if str(rev_dir) in graph_path or "plan_revisions" in graph_path:
                raise PlanRevisionCorruption(
                    f"PLAN_REVISION_GRAPH_MISSING: {graph_path}"
                )
            continue
        expected_hash = str(exec_cfg.get("graph_hash") or "")
        if expected_hash:
            loaded = load_graph(existing)
            if loaded.content_hash != expected_hash:
                raise PlanRevisionCorruption(
                    "PLAN_REVISION_GRAPH_HASH_MISMATCH: "
                    f"{graph_path} hash {loaded.content_hash} != {expected_hash}"
                )
            meta_g = dict(loaded.metadata or {})
            if meta_g.get("plan_revision_id") not in {
                None,
                state.active_plan_revision_id,
            }:
                raise PlanRevisionCorruption(
                    "PLAN_REVISION_CORRUPTION: graph revision id mismatch"
                )
