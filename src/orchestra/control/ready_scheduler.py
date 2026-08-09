"""Minimal ready-subtask scheduler (M4). No TaskPlan rewriting / slow loop."""

from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from orchestra.backends.base import ArtifactRef, BackendSessionRef
from orchestra.cli.validate_graph import build_compiler
from orchestra.communication.ledger import DeliveryRecord
from orchestra.control.backend_usage import (
    BackendUsageRecord,
    append_usage_records,
    collect_usage_from_graph_result,
    exception_usage_record,
)
from orchestra.control.canonical_workspace import (
    CanonicalCommitError,
    CanonicalTaskWorkspaceManager,
)
from orchestra.control.failure import classify_subtask_outcome
from orchestra.control.fast_loop.controller import FastLoopController
from orchestra.control.fast_loop.schemas import CostRecord, FastLoopBudget, WorkspaceChangeSet
from orchestra.control.fast_loop.workspace import GitCandidateWorkspaceManager
from orchestra.control.input_assembler import (
    CommunicationDeliveryBlocked,
    SubtaskInputAssembler,
    SubtaskInputAssemblyError,
)
from orchestra.control.pareto.public_evaluation import record_commit_public_evaluations
from orchestra.control.run_ownership import RunOwnership, RunOwnershipError
from orchestra.control.scheduler_recovery import (
    acquire_lease,
    begin_scheduler_session,
)
from orchestra.control.slow_loop.controller import SlowLoopController
from orchestra.control.slow_loop.evidence import active_block_evidence_key
from orchestra.control.slow_loop.graph_materializer import FutureGraphMaterializer
from orchestra.control.slow_loop.revision import PlanRevisionCorruption
from orchestra.control.slow_loop.schemas import (
    SlowLoopConfig,
    SlowLoopState,
    TaskSchedulingPolicy,
)
from orchestra.control.slow_loop.task_budget import TaskBudgetTracker
from orchestra.control.task_state import (
    BackendSessionRecord,
    GlobalUpdateRecord,
    SchedulerWaveRecord,
    SubtaskAttempt,
    SubtaskFailureReason,
    SubtaskState,
    SubtaskStatus,
    TaskExecutionState,
    WorkspaceCommitRecord,
    WorkspaceCommitStatus,
)
from orchestra.decomposition.schemas import TaskPlan
from orchestra.ir.artifacts import ArtifactBundle, ArtifactEnvelope
from orchestra.ir.graph import OrchestraGraph, load_graph
from orchestra.ir.nodes import NodeKind
from orchestra.runtime.backend import RunContext
from orchestra.runtime.native_async import NativeAsyncRuntime
from orchestra.runtime.state import GraphExecutionResult
from orchestra.runtime.task_checkpoint import TaskCheckpointStore
from orchestra.storage.artifacts import ArtifactStore
from orchestra.workspaces.base import WorkspaceRef


class SubtaskExecutionStatus(StrEnum):
    SUCCESS_PENDING_COMMIT = "success_pending_commit"
    FAILED = "failed"
    SKIPPED = "skipped"
    ALREADY_COMMITTED = "already_committed"
    DELIVERY_BLOCKED = "delivery_blocked"


class SubtaskExecutionResult(BaseModel):
    """Worker-local result; must not mutate shared TaskExecutionState/canonical."""

    model_config = ConfigDict(extra="forbid")

    subtask_id: str
    expected_state_version: int
    base_canonical_revision: str | None = None
    candidate_workspace_ref: str | None = None
    workspace_change_set: WorkspaceChangeSet | None = None
    local_subtask_state: SubtaskState
    produced_artifacts: list[ArtifactEnvelope] = Field(default_factory=list)
    backend_sessions: list[BackendSessionRecord] = Field(default_factory=list)
    candidate_harness_artifact_id: str | None = None
    candidate_harness_passed: bool = False
    execution_status: SubtaskExecutionStatus
    failure_reason: SubtaskFailureReason | None = None
    failure_message: str | None = None
    fast_loop_state: Any | None = None
    fast_loop_history_append: list[Any] = Field(default_factory=list)
    graph_template: str | None = None
    delivery_records_append: list[DeliveryRecord] = Field(default_factory=list)
    backend_usage_append: list[BackendUsageRecord] = Field(default_factory=list)


def _collect_backend_sessions(
    *,
    result: GraphExecutionResult,
    attempt_id: int,
) -> list[BackendSessionRecord]:
    records: list[BackendSessionRecord] = []
    for node_id, meta in result.state.node_backend_metadata.items():
        raw = meta.get("session_ref")
        if not raw:
            continue
        session_ref = BackendSessionRef.model_validate(raw)
        backend_id = str(meta.get("backend_id") or session_ref.backend_id)
        records.append(
            BackendSessionRecord(
                node_id=node_id,
                backend_id=backend_id,
                attempt_id=attempt_id,
                session_ref=session_ref,
                candidate_id=None,
            )
        )
    return records


def _cost_from_graph_result(result: GraphExecutionResult) -> CostRecord:
    prompt = 0
    completion = 0
    calls = 0
    for meta in result.state.node_backend_metadata.values():
        usage = meta.get("usage") or {}
        if isinstance(usage, dict):
            prompt += int(usage.get("prompt_tokens") or 0)
            completion += int(usage.get("completion_tokens") or 0)
        if meta.get("backend_status") or meta.get("session_ref"):
            calls += 1
    if calls == 0:
        calls = 1
    return CostRecord(
        prompt_tokens=prompt,
        completion_tokens=completion,
        estimated_cost_usd=(prompt * 0.15 + completion * 0.60) / 1_000_000,
        backend_calls=calls,
    )


def _harness_command(
    graph: OrchestraGraph,
    *,
    expected_harness_id: str | None = None,
) -> tuple[list[str], float]:
    """Pick canonical-commit harness command from the local graph.

    Prefer the harness matching ``expected_harness_id`` (SubtaskSpec keystone).
    Fall back to the first harness node, then pytest -q.
    """
    harness_nodes = [n for n in graph.nodes if n.node_kind is NodeKind.HARNESS]
    selected = None
    if expected_harness_id:
        for node in harness_nodes:
            if getattr(node, "harness_id", None) == expected_harness_id:
                selected = node
                break
    if selected is None and harness_nodes:
        selected = harness_nodes[0]
    if selected is None:
        return ["python", "-m", "pytest", "-q"], 60.0
    command = list(
        getattr(selected, "command", None) or ["python", "-m", "pytest", "-q"]
    )
    timeout = float(getattr(selected, "timeout_seconds", None) or 60.0)
    return command, timeout


def _change_set_hash(cs: WorkspaceChangeSet | None) -> str:
    if cs is None:
        return ""
    return cs.file_manifest_hash or hashlib.sha256(
        cs.tracked_patch.encode()
    ).hexdigest()


def _change_set_nonempty(cs: WorkspaceChangeSet | None) -> bool:
    if cs is None:
        return False
    return bool(
        cs.tracked_patch.strip()
        or cs.modified_files
        or cs.added_untracked_files
        or cs.deleted_files
        or cs.renamed_files
    )


def _with_prompt_prelude(graph: OrchestraGraph, prelude: str) -> OrchestraGraph:
    """Return a copy whose agent nodes carry runtime context in their prompt."""
    from orchestra.ir.nodes import AgentNodeSpec

    nodes = [
        node.model_copy(update={"prompt_prelude": prelude})
        if isinstance(node, AgentNodeSpec)
        else node
        for node in graph.nodes
    ]
    return graph.model_copy(update={"nodes": nodes})


class ReadySubtaskScheduler:
    """Schedule dependency-ready subtasks; invoke FastLoopController on failure."""

    def __init__(
        self,
        *,
        runtime: NativeAsyncRuntime,
        artifact_store: ArtifactStore,
        task_checkpoint_store: TaskCheckpointStore,
        contracts_dir: str = "configs/contracts",
        fast_loop: FastLoopController | None = None,
        source_repo: str | None = None,
        max_concurrent_subtasks: int = 1,
        budget: FastLoopBudget | None = None,
        allow_concurrent_subtasks: bool = False,
        slow_loop: SlowLoopController | None = None,
        slow_loop_config: SlowLoopConfig | None = None,
        task_budget_tracker: TaskBudgetTracker | None = None,
    ) -> None:
        if max_concurrent_subtasks > 1 and not allow_concurrent_subtasks:
            raise ValueError(
                "max_concurrent_subtasks > 1 requires allow_concurrent_subtasks=True "
                "(M4 uses locked merge of SubtaskExecutionResult)"
            )
        self.runtime = runtime
        self.artifact_store = artifact_store
        self.task_checkpoint_store = task_checkpoint_store
        self.contracts_dir = contracts_dir
        self.source_repo = source_repo
        self.max_concurrent_subtasks = max_concurrent_subtasks
        self._candidate_ws = GitCandidateWorkspaceManager()
        self.canonical = CanonicalTaskWorkspaceManager(self._candidate_ws)
        self.input_assembler = SubtaskInputAssembler(artifact_store)
        self.fast_loop = fast_loop or FastLoopController(
            runtime=runtime,
            artifact_store=artifact_store,
            task_checkpoint_store=task_checkpoint_store,
            contracts_dir=contracts_dir,
            budget=budget,
            workspace_manager=self._candidate_ws,
            persist_checkpoints=False,
        )
        self.slow_loop = slow_loop or SlowLoopController(
            config=slow_loop_config,
            checkpoint_store=task_checkpoint_store,
        )
        self.task_budget_tracker = task_budget_tracker or TaskBudgetTracker()
        self.compiler = build_compiler(contracts_dir)
        self._state_lock = asyncio.Lock()

    def _effective_concurrency(self, state: TaskExecutionState) -> int:
        policy = state.scheduling_policy
        if policy is None:
            return self.max_concurrent_subtasks
        if not isinstance(policy, TaskSchedulingPolicy):
            policy = TaskSchedulingPolicy.model_validate(policy)
        return max(1, min(self.max_concurrent_subtasks, policy.max_concurrent_subtasks))

    async def _deliverable_ready_ids(
        self,
        *,
        state: TaskExecutionState,
        task_plan: TaskPlan,
        initial_artifacts: ArtifactBundle,
    ) -> list[str]:
        """Preflight communication; only deliverable targets may acquire leases.

        Preflight runs over *all* dependency-ready candidates (no concurrency
        cap). Concurrency is applied only after blocked targets are filtered
        out, so a communication-blocked head-of-queue cannot starve others.
        """
        del initial_artifacts
        candidates = self._ordered_ready_candidates(state, apply_limit=False)
        deliverable: list[str] = []
        for sid in candidates:
            sub = state.subtasks[sid]
            preflight = await self.input_assembler.delivery_engine.preflight_for_target(
                task_plan=task_plan,
                task_state=state,
                communication_plan=state.communication_plan,
                target_subtask_id=sid,
                persist_projection=True,
            )
            if preflight.batch and preflight.batch.audit_records:
                state.delivery_ledger.extend(preflight.batch.audit_records)
            if preflight.batch and preflight.batch.new_records:
                state.delivery_ledger.extend(preflight.batch.new_records)
            if preflight.blocked:
                sub.communication_block_reason = (
                    preflight.block_reason.value
                    if preflight.block_reason is not None
                    else "delivery_blocked"
                )
                continue
            sub.communication_block_reason = None
            deliverable.append(sid)
        limit = self._effective_concurrency(state)
        return deliverable[:limit] if limit else deliverable

    def _ordered_ready_candidates(
        self,
        state: TaskExecutionState,
        *,
        apply_limit: bool = True,
    ) -> list[str]:
        state.mark_ready_from_dependencies()
        # Keep communication-blocked READY targets eligible for re-preflight.
        # Block reasons gate leasing via `_deliverable_ready_ids`, not scheduling.
        ready = [
            sid
            for sid, sub in state.subtasks.items()
            if sub.status is SubtaskStatus.READY and sub.lease_status != "leased"
        ]
        policy = state.scheduling_policy
        if isinstance(policy, TaskSchedulingPolicy) or policy is not None:
            if not isinstance(policy, TaskSchedulingPolicy):
                policy = TaskSchedulingPolicy.model_validate(policy)
            # Respect serialization groups: at most one ready member per group.
            blocked: set[str] = set()
            for group in policy.serialization_groups:
                members = [s for s in group if s in ready]
                if len(members) > 1:
                    keep = sorted(
                        members,
                        key=lambda s: (
                            -policy.priority_overrides.get(
                                s, state.subtasks[s].spec.priority
                            ),
                            s,
                        ),
                    )[0]
                    for m in members:
                        if m != keep:
                            blocked.add(m)
            ready = [s for s in ready if s not in blocked]
            ready = sorted(
                ready,
                key=lambda s: (
                    -policy.priority_overrides.get(s, state.subtasks[s].spec.priority),
                    s,
                ),
            )
        else:
            ready = sorted(ready)
        if not apply_limit:
            return ready
        limit = self._effective_concurrency(state)
        return ready[:limit] if limit else ready

    def _ready_ids(self, state: TaskExecutionState) -> list[str]:
        return self._ordered_ready_candidates(state, apply_limit=True)

    def _open_wave(
        self,
        *,
        state: TaskExecutionState,
        context: RunContext,
        subtask_ids: list[str],
        policy_concurrency: int,
        effective_concurrency: int,
    ) -> SchedulerWaveRecord:
        wave_id = (
            f"wave-{state.task_id}-inc{state.scheduler_incarnation}-"
            f"v{state.state_version}-{'-'.join(subtask_ids)}"
        )
        decision_id = None
        pending = getattr(getattr(state, "pareto_state", None), "pending_decision", None)
        if (
            pending is not None
            and getattr(pending, "activated_revision_id", None)
            == state.active_plan_revision_id
            and state.active_plan_revision_id is not None
        ):
            decision_id = pending.decision_id
            affected = list(pending.affected_subtask_ids or [])
            if not affected:
                affected = list(subtask_ids)
            intersects = bool(set(subtask_ids) & set(affected))
            existing_wave_id = getattr(pending, "affected_wave_id", None)
            if intersects and not existing_wave_id:
                # Bind the first eligible post-activation wave exactly once.
                pending.affected_wave_id = wave_id
                pending.affected_subtask_ids = sorted(set(affected) | set(subtask_ids))
                pending.realization_evidence = {
                    **dict(getattr(pending, "realization_evidence", None) or {}),
                    "binding_status": "bound",
                    "affected_wave_id": wave_id,
                    "bound_subtask_ids": sorted(subtask_ids),
                }
            elif intersects and existing_wave_id:
                # Crash/resume continuation: previous bound wave may still be
                # non-terminal. Explicitly rebind to the recovery wave.
                prev = None
                for wave in state.scheduler_wave_records:
                    wid = getattr(wave, "wave_id", None) or (
                        wave.get("wave_id") if isinstance(wave, dict) else None
                    )
                    if wid == existing_wave_id:
                        prev = wave
                        break
                prev_terminal = None
                if prev is not None:
                    prev_terminal = getattr(prev, "terminal_state", None) or (
                        prev.get("terminal_state") if isinstance(prev, dict) else None
                    )
                if prev_terminal != "terminal":
                    if isinstance(prev, SchedulerWaveRecord):
                        prev.terminal_state = "interrupted_recovered"
                    elif isinstance(prev, dict):
                        prev["terminal_state"] = "interrupted_recovered"
                    pending.affected_wave_id = wave_id
                    pending.affected_subtask_ids = sorted(set(affected) | set(subtask_ids))
                    pending.realization_evidence = {
                        **dict(getattr(pending, "realization_evidence", None) or {}),
                        "binding_status": "rebound_recovery_continuation",
                        "previous_affected_wave_id": existing_wave_id,
                        "affected_wave_id": wave_id,
                        "bound_subtask_ids": sorted(subtask_ids),
                    }
        wave = SchedulerWaveRecord(
            wave_id=wave_id,
            run_id=str(getattr(context, "run_id", "") or state.task_id),
            scheduler_incarnation=int(state.scheduler_incarnation),
            plan_revision=state.active_plan_revision_id,
            policy_concurrency=policy_concurrency,
            runtime_cap=int(self.max_concurrent_subtasks),
            effective_concurrency=effective_concurrency,
            subtask_ids=list(subtask_ids),
            started_at=datetime.now(UTC),
            terminal_state="running",
            decision_id=decision_id,
        )
        state.scheduler_wave_records.append(wave)
        state.current_wave_id = wave_id
        return wave

    def _close_wave(self, state: TaskExecutionState, wave_id: str) -> None:
        for idx, wave in enumerate(state.scheduler_wave_records):
            wid = getattr(wave, "wave_id", None) or (
                wave.get("wave_id") if isinstance(wave, dict) else None
            )
            if wid != wave_id:
                continue
            if isinstance(wave, SchedulerWaveRecord):
                wave.completed_at = datetime.now(UTC)
                wave.terminal_state = "terminal"
                state.scheduler_wave_records[idx] = wave
            elif isinstance(wave, dict):
                wave["completed_at"] = datetime.now(UTC).isoformat()
                wave["terminal_state"] = "terminal"
            break
        if state.current_wave_id == wave_id:
            state.current_wave_id = None

    def _deps_failed(self, state: TaskExecutionState, subtask_id: str) -> bool:
        sub = state.subtasks[subtask_id]
        for dep in sub.spec.dependencies:
            dep_state = state.subtasks[dep]
            if dep_state.status in {
                SubtaskStatus.FAILED,
                SubtaskStatus.SKIPPED,
                SubtaskStatus.HARNESS_FAILED,
            }:
                return True
        return False

    async def run_task(
        self,
        task_plan: TaskPlan,
        state: TaskExecutionState,
        *,
        initial_artifacts: ArtifactBundle,
        context: RunContext,
        source_repo: str | None = None,
    ) -> TaskExecutionState:
        # Acquire ownership before any prepare/mutate/checkpoint write.
        ownership = RunOwnership(
            context.run_dir,
            owner_id=f"{context.run_id}:{os.getpid()}",
        )
        try:
            ownership.acquire()
        except RunOwnershipError:
            raise
        try:
            repo = source_repo or self.source_repo
            if repo and not state.canonical_workspace_ref:
                canonical = await self.canonical.prepare(
                    source_repo=repo,
                    run_dir=str(context.run_dir),
                    task_id=state.task_id,
                )
                state.canonical_workspace_ref = canonical.path
                state.canonical_revision = canonical.base_revision
                state.state_version += 1
                await self.task_checkpoint_store.save(state)

            if state.scheduling_policy is None:
                # Default policy matches the constructor runtime cap. Callers that
                # need policy=1 with cap>1 (Stage-2 concurrency adaptation) must
                # set scheduling_policy explicitly before run_task.
                state.scheduling_policy = TaskSchedulingPolicy(
                    max_concurrent_subtasks=self.max_concurrent_subtasks
                )

            return await self._run_task_owned(
                task_plan=task_plan,
                state=state,
                initial_artifacts=initial_artifacts,
                context=context,
                repo=repo,
            )
        finally:
            ownership.release()

    async def _run_task_owned(
        self,
        *,
        task_plan: TaskPlan,
        state: TaskExecutionState,
        initial_artifacts: ArtifactBundle,
        context: RunContext,
        repo: str | None,
    ) -> TaskExecutionState:
        del task_plan
        # New incarnation; recovery event only when interrupted state is reconciled.
        begin_scheduler_session(
            state,
            reason="resume_reclaim" if state.scheduler_incarnation else "run_task_start",
            checkpoint_id=state.active_plan_revision_id,
        )
        state.state_version += 1
        await self.task_checkpoint_store.save(state)

        while True:
            for sid, sub in state.subtasks.items():
                if sub.status is SubtaskStatus.PENDING and self._deps_failed(state, sid):
                    sub.status = SubtaskStatus.SKIPPED
                    sub.failure_message = "blocked by failed dependency"

            ready_candidates = self._ordered_ready_candidates(
                state, apply_limit=False
            )
            if not ready_candidates:
                break

            # Communication preflight before lease (required delivery fail-closed).
            deliverable = await self._deliverable_ready_ids(
                state=state,
                task_plan=state.task_plan,
                initial_artifacts=initial_artifacts,
            )
            if not deliverable:
                recovered = await self._recover_blocked_wave(
                    state=state,
                    context=context,
                    ready_candidates=ready_candidates,
                )
                if recovered:
                    continue
                break

            # Acquire leases only for deliverable targets (future plan freeze).
            leased = set(deliverable)
            for sid in leased:
                acquire_lease(state, sid)
            concurrency = self._effective_concurrency(state)
            policy_conc = 1
            if state.scheduling_policy is not None:
                policy_conc = int(state.scheduling_policy.max_concurrent_subtasks)
            wave = self._open_wave(
                state=state,
                context=context,
                subtask_ids=sorted(leased),
                policy_concurrency=policy_conc,
                effective_concurrency=concurrency,
            )
            state.state_version += 1
            await self.task_checkpoint_store.save(state)

            # Snapshot for workers (deep copy) so they never mutate shared state.
            state_snapshot = state.model_copy(deep=True)
            ready = deliverable
            if concurrency <= 1:
                for sid in ready:
                    result = await self._run_subtask_isolated(
                        task_plan=state.task_plan,
                        state=state_snapshot,
                        subtask_id=sid,
                        initial_artifacts=initial_artifacts,
                        context=context,
                        source_repo=repo,
                        expected_state_version=state.state_version,
                    )
                    await self._commit_subtask_result(
                        task_plan=state.task_plan,
                        state=state,
                        result=result,
                        context=context,
                    )
            else:
                expected = state.state_version
                gathered = await asyncio.gather(
                    *[
                        self._run_subtask_isolated(
                            task_plan=state.task_plan,
                            state=state_snapshot,
                            subtask_id=sid,
                            initial_artifacts=initial_artifacts,
                            context=context,
                            source_repo=repo,
                            expected_state_version=expected,
                        )
                        for sid in ready
                    ]
                )
                for result in sorted(gathered, key=lambda r: r.subtask_id):
                    await self._commit_subtask_result(
                        task_plan=state.task_plan,
                        state=state,
                        result=result,
                        context=context,
                    )

            # Release leases after wave commits and close wave evidence.
            for sid in leased:
                sub = state.subtasks[sid]
                if sub.lease_status == "leased":
                    sub.lease_status = "released"
            self._close_wave(state, wave.wave_id)
            await self.task_checkpoint_store.save(state)

            # Slow Loop only at safe checkpoint between waves.
            async with self._state_lock:
                task_budget = self.task_budget_tracker.snapshot(
                    task_plan=state.task_plan,
                    task_state=state,
                )
                slow_result = await self.slow_loop.maybe_update(
                    task_plan=state.task_plan,
                    state=state,
                    context=context,
                    leased_subtask_ids=set(),
                    task_budget=task_budget,
                )
                if slow_result.updated:
                    state.clear_communication_blocks()
                await self.task_checkpoint_store.save(state)

            if not self._ready_ids(state):
                break

        state.frozen = all(
            s.status is SubtaskStatus.COMMITTED for s in state.subtasks.values()
        )
        state.state_version += 1
        await self.task_checkpoint_store.save(state)
        return state

    async def _recover_blocked_wave(
        self,
        *,
        state: TaskExecutionState,
        context: RunContext,
        ready_candidates: list[str],
    ) -> bool:
        """Persist block evidence, invoke Slow Loop, retry preflight if revised.

        Returns True when the scheduler should retry the wave loop.
        """
        blocked = [
            sid
            for sid in ready_candidates
            if state.subtasks[sid].communication_block_reason
        ]
        state.state_version += 1
        await self.task_checkpoint_store.save(state)

        async with self._state_lock:
            task_budget = self.task_budget_tracker.snapshot(
                task_plan=state.task_plan,
                task_state=state,
            )
            result = await self.slow_loop.maybe_update(
                task_plan=state.task_plan,
                state=state,
                context=context,
                leased_subtask_ids=set(),
                task_budget=task_budget,
            )
            if result.updated:
                state.clear_communication_blocks()
                state.state_version += 1
                await self.task_checkpoint_store.save(state)
                return True

            handled: set[str] = set()
            if state.slow_loop_state is not None:
                slow = (
                    state.slow_loop_state
                    if isinstance(state.slow_loop_state, SlowLoopState)
                    else SlowLoopState.model_validate(state.slow_loop_state)
                )
                handled = set(slow.handled_evidence_keys)
            block_keys = [
                active_block_evidence_key(
                    state=state,
                    target_subtask_id=sid,
                    reason=state.subtasks[sid].communication_block_reason or "",
                )
                for sid in blocked
                if state.subtasks[sid].communication_block_reason
            ]
            evidence_consumed = bool(block_keys) and all(k in handled for k in block_keys)
            fail_closed = (
                result.message
                in {
                    "NO_SAFE_FUTURE_EDIT",
                    "NO_COMPARABLE_PARETO_CANDIDATE",
                    "slow_loop disabled",
                    "max_updates_per_task exhausted",
                    "no trigger",
                    "diagnosis NO_CHANGE",
                }
                or evidence_consumed
                or not result.updated
            )
            if fail_closed:
                self._terminate_blocked_wave(
                    state,
                    blocked_ids=blocked,
                    message=result.message or "COMMUNICATION_BLOCKED_UNRECOVERABLE",
                )
                state.state_version += 1
                await self.task_checkpoint_store.save(state)
            return False

    def _terminate_blocked_wave(
        self,
        state: TaskExecutionState,
        *,
        blocked_ids: list[str],
        message: str,
    ) -> None:
        """Fail-closed audited outcome for unrecoverable communication blocks."""
        summary = f"COMMUNICATION_BLOCKED_UNRECOVERABLE: {message}"
        for sid in blocked_ids:
            sub = state.subtasks[sid]
            if sub.status not in {SubtaskStatus.READY, SubtaskStatus.PENDING}:
                continue
            if not sub.communication_block_reason:
                continue
            sub.status = SubtaskStatus.FAILED
            sub.failure_reason = SubtaskFailureReason.INVALID_CONFIG
            sub.failure_message = (
                f"{summary}; target={sid}; reason={sub.communication_block_reason}"
            )
            sub.lease_status = "unleased"
        # Skip remaining dependency-blocked pending work for a clean freeze.
        for sid, sub in state.subtasks.items():
            if sub.status is SubtaskStatus.PENDING and self._deps_failed(state, sid):
                sub.status = SubtaskStatus.SKIPPED
                sub.failure_message = "blocked by failed dependency"
        state.slow_loop_history.append(
            GlobalUpdateRecord(
                record_id=f"blocked-wave-{state.state_version}-{uuid.uuid4().hex[:8]}",
                revision=state.global_revision,
                summary=summary,
                metadata={
                    "blocked_ids": list(blocked_ids),
                    "block_reasons": {
                        sid: state.subtasks[sid].communication_block_reason
                        for sid in blocked_ids
                    },
                },
            )
        )

    async def _commit_subtask_result(
        self,
        *,
        task_plan: TaskPlan,
        state: TaskExecutionState,
        result: SubtaskExecutionResult,
        context: RunContext,
    ) -> None:
        async with self._state_lock:
            await self._commit_subtask_result_unlocked(
                task_plan=task_plan,
                state=state,
                result=result,
                context=context,
            )
            await self.task_checkpoint_store.save(state)

    async def _commit_subtask_result_unlocked(
        self,
        *,
        task_plan: TaskPlan,
        state: TaskExecutionState,
        result: SubtaskExecutionResult,
        context: RunContext,
    ) -> None:
        del task_plan
        sid = result.subtask_id
        current = state.subtasks[sid]
        if current.status is SubtaskStatus.COMMITTED:
            return

        # Idempotent recovery: prior COMMITTED record for same change_set_hash.
        cs_hash = _change_set_hash(result.workspace_change_set)
        for rec in state.workspace_commit_records:
            if (
                rec.subtask_id == sid
                and rec.status is WorkspaceCommitStatus.COMMITTED
                and rec.change_set_hash == cs_hash
                and cs_hash
            ):
                sub = result.local_subtask_state.model_copy(deep=True)
                sub.status = SubtaskStatus.COMMITTED
                sub.failure_reason = None
                sub.failure_message = None
                if sub.candidate_artifacts and not sub.committed_artifacts:
                    sub.committed_artifacts = list(sub.candidate_artifacts)
                sub.last_commit_record_id = rec.record_id
                state.subtasks[sid] = sub
                if rec.committed_revision:
                    state.canonical_revision = rec.committed_revision
                self._merge_fast_loop(state, result)
                state.mark_ready_from_dependencies()
                state.state_version += 1
                return

        if result.execution_status is SubtaskExecutionStatus.ALREADY_COMMITTED:
            state.subtasks[sid] = result.local_subtask_state
            self._merge_fast_loop(state, result)
            state.mark_ready_from_dependencies()
            state.state_version += 1
            return

        if result.execution_status is SubtaskExecutionStatus.DELIVERY_BLOCKED:
            sub = result.local_subtask_state.model_copy(deep=True)
            sub.status = SubtaskStatus.READY
            sub.lease_status = "released"
            sub.failure_reason = None
            state.subtasks[sid] = sub
            self._merge_fast_loop(state, result)
            state.state_version += 1
            return

        if result.execution_status is SubtaskExecutionStatus.FAILED:
            state.subtasks[sid] = result.local_subtask_state
            self._merge_fast_loop(state, result)
            state.mark_ready_from_dependencies()
            state.state_version += 1
            return

        if result.execution_status is SubtaskExecutionStatus.SKIPPED:
            state.subtasks[sid] = result.local_subtask_state
            state.mark_ready_from_dependencies()
            state.state_version += 1
            return

        # SUCCESS_PENDING_COMMIT
        sub = result.local_subtask_state.model_copy(deep=True)
        attempt = sub.attempts[-1] if sub.attempts else None
        attempt_id = int(attempt.attempt_id) if attempt is not None else (len(sub.attempts) or 1)
        pending = getattr(getattr(state, "pareto_state", None), "pending_decision", None)
        decision_id = getattr(pending, "decision_id", None) if pending is not None else None
        record = WorkspaceCommitRecord(
            record_id=f"{state.task_id}:{sid}:{attempt_id}:{uuid.uuid4().hex[:8]}",
            task_id=state.task_id,
            subtask_id=sid,
            attempt_id=attempt_id,
            expected_base_revision=result.base_canonical_revision,
            actual_parent_revision=state.canonical_revision,
            change_set_hash=cs_hash,
            applied_artifact_ids=[a.artifact_id for a in result.produced_artifacts],
            status=WorkspaceCommitStatus.PENDING,
            run_id=str(context.run_id or ""),
            lease_id=(
                getattr(attempt, "lease_id", None) if attempt is not None else sub.lease_id
            ),
            wave_id=(
                getattr(attempt, "wave_id", None)
                if attempt is not None
                else state.current_wave_id
            ),
            execution_plan_revision=(
                getattr(attempt, "execution_plan_revision", None)
                if attempt is not None
                else state.active_plan_revision_id
            ),
            scheduler_incarnation=(
                getattr(attempt, "scheduler_incarnation", None)
                if attempt is not None
                else int(state.scheduler_incarnation or 0)
            ),
            decision_id=decision_id,
            usage_ids=list(getattr(attempt, "usage_ids", None) or []),
            evaluation_ids=list(getattr(attempt, "evaluation_ids", None) or []),
            provenance="ready_scheduler_commit",
        )
        state.workspace_commit_records.append(record)

        needs_repo = bool(
            state.canonical_workspace_ref
            and result.candidate_workspace_ref
            and _change_set_nonempty(result.workspace_change_set)
        )

        if not needs_repo:
            # Artifact-only / no dirty repo → mark committed without workspace mutate.
            sub.status = SubtaskStatus.COMMITTED
            sub.failure_reason = None
            sub.failure_message = None
            if sub.candidate_artifacts and not sub.committed_artifacts:
                sub.committed_artifacts = list(sub.candidate_artifacts)
            record.status = WorkspaceCommitStatus.COMMITTED
            record.committed_revision = state.canonical_revision
            record.terminal_state = "committed"
            record.committed_at = datetime.now(UTC)
            sub.last_commit_record_id = record.record_id
            state.subtasks[sid] = sub
            state.committed_subtask_count += 1
            self._merge_fast_loop(state, result)
            self._stamp_commit_identity_from_attempt(state, sid, record)
            state.clear_communication_blocks()
            state.mark_ready_from_dependencies()
            state.state_version += 1
            self._record_public_evaluation(state, result, context)
            return

        assert state.canonical_workspace_ref is not None
        assert result.candidate_workspace_ref is not None
        assert result.workspace_change_set is not None

        canonical = WorkspaceRef(
            workspace_id=f"{state.task_id}/canonical",
            path=state.canonical_workspace_ref,
            kind="CANONICAL_TASK_WORKSPACE",
            task_id=state.task_id,
            subtask_id="__canonical__",
            base_revision=state.canonical_revision,
        )
        winner = WorkspaceRef(
            workspace_id=f"{state.task_id}/{sid}/pending",
            path=result.candidate_workspace_ref,
            kind="SHARED_SUBTASK_WORKSPACE",
            task_id=state.task_id,
            subtask_id=sid,
            base_revision=result.base_canonical_revision,
        )
        graph = load_graph(result.graph_template or sub.spec.local_graph_template)
        command, timeout = _harness_command(
            graph, expected_harness_id=sub.spec.keystone_harness_id
        )

        record.status = WorkspaceCommitStatus.APPLYING
        try:
            record.status = WorkspaceCommitStatus.VALIDATING
            promoted, new_rev = await self.canonical.transactional_commit(
                canonical=canonical,
                winner_workspace=winner,
                change_set=result.workspace_change_set,
                run_dir=str(context.run_dir),
                task_id=state.task_id,
                subtask_id=sid,
                attempt_id=attempt_id,
                commit_message=f"canonical commit {sid}",
                harness_command=command,
                harness_timeout=timeout,
            )
        except CanonicalCommitError as exc:
            if exc.conflict:
                record.status = WorkspaceCommitStatus.CONFLICTED
                record.conflict_files = list(exc.conflict_files)
                record.error_message = str(exc)
                sub.status = SubtaskStatus.FAILED
                sub.failure_reason = SubtaskFailureReason.CANONICAL_MERGE_CONFLICT
                sub.failure_message = str(exc)
                sub.committed_artifacts = []
            elif exc.validation_failed:
                record.status = WorkspaceCommitStatus.VALIDATION_FAILED
                record.error_message = str(exc)
                sub.status = SubtaskStatus.FAILED
                sub.failure_reason = SubtaskFailureReason.CANONICAL_VALIDATION_FAILED
                sub.failure_message = str(exc)
                sub.committed_artifacts = []
            else:
                record.status = WorkspaceCommitStatus.FAILED
                record.error_message = str(exc)
                sub.status = SubtaskStatus.FAILED
                sub.failure_reason = SubtaskFailureReason.INFRA
                sub.failure_message = str(exc)
                sub.committed_artifacts = []
            sub.last_commit_record_id = record.record_id
            state.subtasks[sid] = sub
            self._merge_fast_loop(state, result)
            state.mark_ready_from_dependencies()
            state.state_version += 1
            return

        record.status = WorkspaceCommitStatus.COMMITTED
        record.committed_revision = new_rev or promoted.base_revision
        record.actual_parent_revision = state.canonical_revision
        record.terminal_state = "committed"
        record.committed_at = datetime.now(UTC)
        state.canonical_workspace_ref = promoted.path
        state.canonical_revision = record.committed_revision
        # Cross-milestone memory lives in the run directory and reaches later
        # milestones through their prompt, never through the workspace.
        try:
            self._append_milestone_memory(
                run_dir=context.run_dir,
                subtask_id=sid,
                role=str(
                    sub.spec.metadata.get("public_harness_level")
                    or sub.spec.metadata.get("role")
                    or ""
                ),
                change_set=result.workspace_change_set,
                revision=record.committed_revision,
            )
        except Exception:  # noqa: BLE001 — memory must not fail the commit
            pass
        sub.status = SubtaskStatus.COMMITTED
        state.committed_subtask_count += 1
        sub.failure_reason = None
        sub.failure_message = None
        sub.base_task_revision = state.canonical_revision
        if sub.candidate_artifacts and not sub.committed_artifacts:
            sub.committed_artifacts = list(sub.candidate_artifacts)
        elif not sub.committed_artifacts and sub.final_output_artifact_id:
            # Preserve prior FastLoop / first-pass artifact refs as committed.
            pass
        # Promote candidate artifact refs into committed if worker stashed them.
        if result.produced_artifacts and not sub.committed_artifacts:
            art = result.produced_artifacts[0]
            sub.committed_artifacts = [
                ArtifactRef(
                    slot="final",
                    artifact_id=art.artifact_id,
                    artifact_type=art.artifact_type,
                )
            ]
            sub.final_output_artifact_id = art.artifact_id
        sub.last_commit_record_id = record.record_id
        state.subtasks[sid] = sub
        self._merge_fast_loop(state, result)
        self._stamp_commit_identity_from_attempt(state, sid, record)
        state.clear_communication_blocks()
        state.mark_ready_from_dependencies()
        state.state_version += 1
        self._record_public_evaluation(state, result, context)

    @staticmethod
    def _milestone_prompt_prelude(
        *,
        subtask_metadata: dict[str, Any],
        run_dir: str | Path | None,
    ) -> str:
        """Milestone brief + earlier-milestone memory, delivered as prompt text."""
        brief = subtask_metadata.get("milestone_brief")
        if not isinstance(brief, str) or not brief.strip():
            return ""
        from orchestra.realbench.workspace_memory import (
            memory_brief_for_milestone,
            memory_dir_for_run,
        )

        memory = (
            memory_brief_for_milestone(memory_dir_for_run(Path(run_dir)))
            if run_dir
            else ""
        )
        return brief.rstrip() + ("\n\n" + memory if memory else "\n")

    @staticmethod
    def _append_milestone_memory(
        *,
        run_dir: str | Path | None,
        subtask_id: str,
        role: str,
        change_set: WorkspaceChangeSet | None,
        revision: str | None,
    ) -> None:
        if not run_dir:
            return
        from orchestra.realbench.workspace_memory import (
            append_changelog_entry,
            memory_dir_for_run,
        )

        files: list[str] = []
        if change_set is not None:
            files.extend(change_set.modified_files or [])
            files.extend(change_set.added_untracked_files or [])
            files.extend(change_set.deleted_files or [])
            for ren in change_set.renamed_files or []:
                files.append(getattr(ren, "to_path", None) or getattr(ren, "from_path", ""))
        append_changelog_entry(
            memory_dir_for_run(Path(run_dir)),
            subtask_id=subtask_id,
            role=role or None,
            changed_files=[f for f in files if f],
            revision=revision,
            summary=f"canonical commit for subtask {subtask_id}",
        )

    @staticmethod
    def _stamp_commit_identity_from_attempt(
        state: TaskExecutionState,
        subtask_id: str,
        record: WorkspaceCommitRecord,
    ) -> None:
        """Mirror attempt execution identity onto the commit after usage merge."""
        sub = state.subtasks.get(subtask_id)
        if sub is None or not sub.attempts:
            return
        att = sub.attempts[-1]
        if att.usage_ids:
            record.usage_ids = list(att.usage_ids)
        if att.evaluation_ids:
            record.evaluation_ids = list(att.evaluation_ids)
        if att.lease_id:
            record.lease_id = att.lease_id
        if att.wave_id:
            record.wave_id = att.wave_id
        if att.execution_plan_revision:
            record.execution_plan_revision = att.execution_plan_revision
        if att.scheduler_incarnation is not None:
            record.scheduler_incarnation = att.scheduler_incarnation
        if record.terminal_state is None and record.status is WorkspaceCommitStatus.COMMITTED:
            record.terminal_state = "committed"

    def _record_public_evaluation(
        self,
        state: TaskExecutionState,
        result: SubtaskExecutionResult,
        context: RunContext,
    ) -> None:
        """Persist public harness evidence for production Pareto quality estimation."""
        if state.subtasks[result.subtask_id].status is not SubtaskStatus.COMMITTED:
            return
        record_commit_public_evaluations(
            state=state,
            run_id=str(getattr(context, "run_id", "") or state.task_id),
            subtask_id=result.subtask_id,
            produced_artifacts=list(result.produced_artifacts or []),
            candidate_harness_passed=bool(result.candidate_harness_passed),
            run_dir=getattr(context, "run_dir", None),
        )

    def _merge_fast_loop(
        self, state: TaskExecutionState, result: SubtaskExecutionResult
    ) -> None:
        if result.fast_loop_state is not None:
            state.fast_loop_states[result.subtask_id] = result.fast_loop_state
        for item in result.fast_loop_history_append:
            state.fast_loop_history.append(item)
        for rec in result.delivery_records_append:
            state.delivery_ledger.append(rec)
        stamped: list[BackendUsageRecord] = []
        phase = (
            "post_activation"
            if state.active_plan_revision_id
            else "pre_activation"
        )
        pending = getattr(getattr(state, "pareto_state", None), "pending_decision", None)
        decision_id = None
        if (
            pending is not None
            and getattr(pending, "activated_revision_id", None)
            == state.active_plan_revision_id
            and state.active_plan_revision_id is not None
        ):
            decision_id = getattr(pending, "decision_id", None)
        run_id = str(getattr(state, "task_id", "") or "")
        for rec in list(result.backend_usage_append or []):
            if not isinstance(rec, BackendUsageRecord):
                rec = BackendUsageRecord.model_validate(rec)
            updates: dict[str, Any] = {}
            if not rec.run_id:
                updates["run_id"] = run_id
            if not rec.wave_id:
                updates["wave_id"] = state.current_wave_id
            if not rec.plan_revision:
                updates["plan_revision"] = state.active_plan_revision_id
            if rec.scheduler_incarnation is None:
                updates["scheduler_incarnation"] = state.scheduler_incarnation
            if not rec.decision_id and decision_id:
                updates["decision_id"] = decision_id
            if not rec.phase or rec.phase == "pre_activation":
                updates["phase"] = phase
            if not rec.backend_kind:
                updates["backend_kind"] = rec.backend_id
            if rec.total_tokens is None and (
                rec.prompt_tokens is not None or rec.completion_tokens is not None
            ):
                updates["total_tokens"] = int(rec.prompt_tokens or 0) + int(
                    rec.completion_tokens or 0
                )
            stamped.append(rec.model_copy(update=updates) if updates else rec)
        state.backend_usage_records = append_usage_records(
            list(state.backend_usage_records or []),
            stamped,
        )
        # Mirror usage identity onto the committed attempt when present.
        sub = state.subtasks.get(result.subtask_id)
        if sub is not None and sub.attempts and stamped:
            last = sub.attempts[-1]
            usage_ids = sorted({r.usage_id for r in stamped if r.usage_id})
            updates_att: dict[str, Any] = {}
            if not last.usage_ids:
                updates_att["usage_ids"] = usage_ids
            if not last.wave_id:
                updates_att["wave_id"] = state.current_wave_id
            if not last.execution_plan_revision:
                updates_att["execution_plan_revision"] = state.active_plan_revision_id
            if last.scheduler_incarnation is None:
                updates_att["scheduler_incarnation"] = state.scheduler_incarnation
            if not last.lease_id:
                updates_att["lease_id"] = sub.lease_id
            if updates_att:
                sub.attempts[-1] = last.model_copy(update=updates_att)
                state.subtasks[result.subtask_id] = sub

    async def _run_subtask_isolated(
        self,
        *,
        task_plan: TaskPlan,
        state: TaskExecutionState,
        subtask_id: str,
        initial_artifacts: ArtifactBundle,
        context: RunContext,
        source_repo: str | None,
        expected_state_version: int = -1,
    ) -> SubtaskExecutionResult:
        """Execute one subtask without mutating shared state or canonical repo."""
        del task_plan
        sub = state.subtasks[subtask_id].model_copy(deep=True)
        if sub.status is SubtaskStatus.COMMITTED:
            return SubtaskExecutionResult(
                subtask_id=subtask_id,
                expected_state_version=expected_state_version,
                local_subtask_state=sub,
                execution_status=SubtaskExecutionStatus.ALREADY_COMMITTED,
                candidate_harness_passed=True,
            )

        ledger_before = len(state.delivery_ledger)
        usage_before = len(state.backend_usage_records or [])
        try:
            assembled = await self.input_assembler.assemble(
                task_plan=state.task_plan,
                task_state=state,
                subtask=sub.spec,
                root_artifacts=initial_artifacts,
            )
        except CommunicationDeliveryBlocked as exc:
            # Fail-closed for required payloads: do not lease/execute backend.
            sub.status = SubtaskStatus.READY
            sub.failure_reason = None
            sub.communication_block_reason = exc.reason
            sub.failure_message = str(exc)
            return SubtaskExecutionResult(
                subtask_id=subtask_id,
                expected_state_version=expected_state_version,
                local_subtask_state=sub,
                execution_status=SubtaskExecutionStatus.DELIVERY_BLOCKED,
                failure_message=str(exc),
                delivery_records_append=list(state.delivery_ledger[ledger_before:]),
            )
        except SubtaskInputAssemblyError as exc:
            sub.status = SubtaskStatus.FAILED
            sub.failure_reason = SubtaskFailureReason.INVALID_CONFIG
            sub.failure_message = str(exc)
            return SubtaskExecutionResult(
                subtask_id=subtask_id,
                expected_state_version=expected_state_version,
                local_subtask_state=sub,
                execution_status=SubtaskExecutionStatus.FAILED,
                failure_reason=SubtaskFailureReason.INVALID_CONFIG,
                failure_message=str(exc),
            )

        sub.applied_dependency_artifact_ids = (
            self.input_assembler.declared_dependency_artifact_ids(
                task_state=state, subtask=sub.spec
            )
        )

        workspace_ref = sub.workspace_ref
        base_task_revision = state.canonical_revision
        if source_repo and state.canonical_workspace_ref:
            canonical = WorkspaceRef(
                workspace_id=f"{state.task_id}/canonical",
                path=state.canonical_workspace_ref,
                kind="CANONICAL_TASK_WORKSPACE",
                task_id=state.task_id,
                subtask_id="__canonical__",
                base_revision=state.canonical_revision,
            )
            prepared = await self.canonical.fork_subtask_workspace(
                canonical=canonical,
                run_dir=str(context.run_dir),
                task_id=state.task_id,
                subtask_id=subtask_id,
            )
            workspace_ref = prepared.path
            base_task_revision = prepared.base_revision
            sub.workspace_ref = workspace_ref
            sub.base_task_revision = base_task_revision
            sub.dependency_revision_ids = [
                state.subtasks[d].base_task_revision or state.canonical_revision or ""
                for d in sorted(sub.spec.dependencies)
                if state.subtasks[d].status is SubtaskStatus.COMMITTED
            ]
        # Milestone brief + cross-milestone memory reach the agent through its
        # prompt only; the workspace stays free of AdaMAS bookkeeping files.
        prompt_prelude = self._milestone_prompt_prelude(
            subtask_metadata=sub.spec.metadata, run_dir=context.run_dir
        )

        exec_cfg = dict(sub.spec.metadata.get("execution_config") or {})
        graph_path = str(
            exec_cfg.get("graph_path")
            or sub.spec.metadata.get("materialized_graph_path")
            or sub.spec.local_graph_template
        )
        if ".staging-" in graph_path.replace("\\", "/"):
            raise PlanRevisionCorruption(
                "PLAN_REVISION_GRAPH_PATH_INVALID: staging path in active plan"
            )
        from pathlib import Path as _Path

        path_obj = _Path(graph_path)
        materialized = bool(
            exec_cfg.get("graph_path")
            or sub.spec.metadata.get("materialized_graph_path")
            or "plan_revisions" in graph_path
        )
        if materialized and not path_obj.exists():
            raise PlanRevisionCorruption(
                f"PLAN_REVISION_GRAPH_MISSING: {graph_path}"
            )
        # Live-materialize only when assignment metadata exists and no snapshot yet.
        if (
            not materialized
            and (
                sub.spec.metadata.get("backend_assignment")
                or sub.spec.metadata.get("model_assignment")
            )
        ):
            mat = FutureGraphMaterializer(compiler=self.compiler).materialize(
                subtask=sub.spec,
                revision_id=state.active_plan_revision_id or "live",
            )
            graph = mat.graph
            graph_path = mat.graph_path or graph_path
        else:
            graph = load_graph(graph_path)
            expected_hash = str(exec_cfg.get("graph_hash") or "")
            if expected_hash and graph.content_hash != expected_hash:
                raise PlanRevisionCorruption(
                    "PLAN_REVISION_GRAPH_HASH_MISMATCH: "
                    f"{graph_path} hash {graph.content_hash} != {expected_hash}"
                )
        if prompt_prelude:
            graph = _with_prompt_prelude(graph, prompt_prelude)
        compiled = self.compiler.compile(graph)
        attempt_id = len(sub.attempts) + 1
        started = datetime.now(UTC)
        sub.status = SubtaskStatus.RUNNING
        sub.failure_reason = None
        sub.failure_message = None
        sub.communication_block_reason = None
        sub.current_graph_hash = graph.content_hash
        sub.attempts.append(
            SubtaskAttempt(
                attempt_id=attempt_id,
                status=SubtaskStatus.RUNNING,
                started_at=started,
                graph_hash=graph.content_hash,
                lease_id=sub.lease_id,
                wave_id=state.current_wave_id,
                execution_plan_revision=state.active_plan_revision_id,
                scheduler_incarnation=int(state.scheduler_incarnation or 0),
            )
        )

        # Isolate graph-level checkpoints per subtask so distinct local graphs
        # (and Slow Loop materializations) cannot collide on CheckpointStore keys.
        run_context = RunContext(
            run_id=f"{context.run_id}:{subtask_id}:{attempt_id}",
            task_id=f"{context.task_id}__subtask__{subtask_id}",
            run_dir=context.run_dir,
            limits=context.limits,
            semaphores=context.semaphores,
            contract_hash=context.contract_hash,
            allow_config_drift=context.allow_config_drift,
            subtask_id=subtask_id,
            workspace_ref=workspace_ref,
        )

        local_state = state.model_copy(deep=True)
        local_state.subtasks[subtask_id] = sub

        try:
            result = await self.runtime.execute(
                graph=compiled,
                initial_artifacts=assembled,
                context=run_context,
            )
        except Exception as exc:  # noqa: BLE001
            status, reason, message = await classify_subtask_outcome(
                result=None,
                artifact_store=self.artifact_store,
                error=exc,
            )
            finished_exc = datetime.now(UTC)
            sub.status = status
            sub.failure_reason = reason
            sub.failure_message = message
            sub.attempts[-1].status = status
            sub.attempts[-1].finished_at = finished_exc
            sub.attempts[-1].error = message
            local_state.subtasks[subtask_id] = sub
            local_state.backend_usage_records = append_usage_records(
                list(local_state.backend_usage_records or []),
                [
                    exception_usage_record(
                        task_id=local_state.task_id,
                        subtask_id=subtask_id,
                        attempt_id=attempt_id,
                        started_at=started,
                        finished_at=finished_exc,
                        status=status.value,
                        accounting_source="ready_scheduler_exception",
                    )
                ],
            )
            local_state = await self.fast_loop.run(
                state=local_state,
                subtask_id=subtask_id,
                context=context,
                initial_artifacts=assembled,
                source_repo=source_repo,
                graph=graph,
                initial_execution_cost=CostRecord(backend_calls=1),
            )
            return await self._finalize_worker_result(
                local_state=local_state,
                subtask_id=subtask_id,
                expected_state_version=expected_state_version,
                base_canonical_revision=base_task_revision,
                graph=graph,
                delivery_records=list(state.delivery_ledger[ledger_before:]),
                usage_before=usage_before,
            )

        finished = datetime.now(UTC)
        sub.attempts[-1].finished_at = finished
        sessions = _collect_backend_sessions(result=result, attempt_id=attempt_id)
        sub.backend_sessions.extend(sessions)
        usage_records = collect_usage_from_graph_result(
            task_id=local_state.task_id,
            subtask_id=subtask_id,
            attempt_id=attempt_id,
            result=result,
            candidate_id=None,
            started_at=started,
            finished_at=finished,
            status="executed",
            accounting_source="ready_scheduler",
        )
        # Stamp scheduler identity onto worker-local usage before merge.
        pending = getattr(getattr(state, "pareto_state", None), "pending_decision", None)
        decision_id = None
        if (
            pending is not None
            and getattr(pending, "activated_revision_id", None)
            == state.active_plan_revision_id
        ):
            decision_id = getattr(pending, "decision_id", None)
        stamped_usage: list[BackendUsageRecord] = []
        for rec in usage_records:
            stamped_usage.append(
                rec.model_copy(
                    update={
                        "run_id": str(getattr(context, "run_id", "") or state.task_id),
                        "wave_id": state.current_wave_id,
                        "plan_revision": state.active_plan_revision_id,
                        "scheduler_incarnation": int(state.scheduler_incarnation or 0),
                        "decision_id": decision_id,
                        "phase": (
                            "post_activation"
                            if state.active_plan_revision_id
                            else "pre_activation"
                        ),
                        "backend_kind": rec.backend_kind or rec.backend_id,
                    }
                )
            )
        local_state.backend_usage_records = append_usage_records(
            list(local_state.backend_usage_records or []), stamped_usage
        )
        sub.attempts[-1].usage_ids = [r.usage_id for r in stamped_usage]
        status, reason, message = await classify_subtask_outcome(
            result=result,
            artifact_store=self.artifact_store,
            error=None,
        )
        sub.status = status
        sub.attempts[-1].status = status
        initial_cost = _cost_from_graph_result(result)

        if status is SubtaskStatus.COMMITTED:
            final_id = result.state.final_output_artifact_id
            assert final_id is not None
            final_artifact = await self.artifact_store.get(final_id)
            sub.final_output_artifact_id = final_id
            # Stash as candidate artifacts; coordinator publishes committed.
            # Namespace by subtask_id so multi-dep assemblers do not collide
            # on equal-priority implicit slots.
            sub.candidate_artifacts = [
                ArtifactRef(
                    slot=f"{compiled.graph.final_output_slot}:{subtask_id}",
                    artifact_id=final_id,
                    artifact_type=final_artifact.artifact_type,
                )
            ]
            sub.committed_artifacts = []
            sub.failure_reason = None
            sub.failure_message = None
            sub.attempts[-1].error = None
            local_state.subtasks[subtask_id] = sub
            return await self._finalize_worker_result(
                local_state=local_state,
                subtask_id=subtask_id,
                expected_state_version=expected_state_version,
                base_canonical_revision=base_task_revision,
                graph=graph,
                produced=[final_artifact],
                sessions=sessions,
                harness_passed=True,
                delivery_records=list(state.delivery_ledger[ledger_before:]),
                usage_before=usage_before,
            )

        sub.failure_reason = reason
        sub.failure_message = message
        sub.attempts[-1].error = message
        sub.status = (
            SubtaskStatus.RETRY_PENDING
            if status is SubtaskStatus.HARNESS_FAILED
            else status
        )
        local_state.subtasks[subtask_id] = sub

        base_ws = None
        base_source = state.canonical_workspace_ref or source_repo
        if base_source:
            base_ws = await self._candidate_ws.prepare_base_snapshot(
                source_repo=base_source,
                run_dir=str(context.run_dir),
                task_id=state.task_id,
                subtask_id=subtask_id,
            )

        local_state = await self.fast_loop.run(
            state=local_state,
            subtask_id=subtask_id,
            context=context,
            initial_artifacts=assembled,
            source_repo=source_repo,
            graph=graph,
            graph_result=result,
            initial_execution_cost=initial_cost,
            base_workspace=base_ws,
        )
        return await self._finalize_worker_result(
            local_state=local_state,
            subtask_id=subtask_id,
            expected_state_version=expected_state_version,
            base_canonical_revision=base_task_revision,
            graph=graph,
            sessions=sessions,
            delivery_records=list(state.delivery_ledger[ledger_before:]),
            usage_before=usage_before,
        )

    async def _finalize_worker_result(
        self,
        *,
        local_state: TaskExecutionState,
        subtask_id: str,
        expected_state_version: int,
        base_canonical_revision: str | None,
        graph: OrchestraGraph,
        produced: list[ArtifactEnvelope] | None = None,
        sessions: list[BackendSessionRecord] | None = None,
        harness_passed: bool | None = None,
        delivery_records: list[DeliveryRecord] | None = None,
        usage_before: int = 0,
    ) -> SubtaskExecutionResult:
        sub = local_state.subtasks[subtask_id].model_copy(deep=True)
        # Never leave worker-owned COMMITTED when a repo coordinate path exists.
        if sub.status is SubtaskStatus.COMMITTED:
            if sub.committed_artifacts and not sub.candidate_artifacts:
                sub.candidate_artifacts = list(sub.committed_artifacts)
            sub.committed_artifacts = []
            sub.status = SubtaskStatus.AWAITING_CANONICAL_COMMIT
            exec_status = SubtaskExecutionStatus.SUCCESS_PENDING_COMMIT
            passed = True if harness_passed is None else harness_passed
        elif sub.status in {
            SubtaskStatus.FAILED,
            SubtaskStatus.HARNESS_FAILED,
            SubtaskStatus.SKIPPED,
        }:
            exec_status = SubtaskExecutionStatus.FAILED
            passed = False
        else:
            exec_status = SubtaskExecutionStatus.FAILED
            passed = False

        change_set: WorkspaceChangeSet | None = None
        workspace_path = sub.workspace_ref
        if (
            exec_status is SubtaskExecutionStatus.SUCCESS_PENDING_COMMIT
            and workspace_path
            and base_canonical_revision
        ):
            winner = WorkspaceRef(
                workspace_id=f"{local_state.task_id}/{subtask_id}/worker",
                path=workspace_path,
                kind="SHARED_SUBTASK_WORKSPACE",
                task_id=local_state.task_id,
                subtask_id=subtask_id,
                base_revision=base_canonical_revision,
            )
            # Dirty working tree = first-pass edits; clean tree with commits
            # since base = FastLoop finalize. Prefer dirty when present so
            # uncommitted tracked edits are not dropped.
            dirty = await self._candidate_ws.collect_changeset(winner)
            if _change_set_nonempty(dirty):
                change_set = dirty
            else:
                try:
                    change_set = await self._candidate_ws.collect_changeset_since(
                        winner, base_canonical_revision
                    )
                except Exception:  # noqa: BLE001
                    change_set = dirty

        history = [h for h in local_state.fast_loop_history if h.subtask_id == subtask_id]
        usage_append = list(local_state.backend_usage_records or [])[usage_before:]
        return SubtaskExecutionResult(
            subtask_id=subtask_id,
            expected_state_version=expected_state_version,
            base_canonical_revision=base_canonical_revision,
            candidate_workspace_ref=workspace_path,
            workspace_change_set=change_set,
            local_subtask_state=sub,
            produced_artifacts=list(produced or []),
            backend_sessions=list(sessions or sub.backend_sessions),
            candidate_harness_passed=passed,
            execution_status=exec_status,
            failure_reason=sub.failure_reason,
            failure_message=sub.failure_message,
            fast_loop_state=local_state.fast_loop_states.get(subtask_id),
            fast_loop_history_append=history,
            graph_template=sub.spec.local_graph_template,
            delivery_records_append=list(delivery_records or []),
            backend_usage_append=usage_append,
        )
