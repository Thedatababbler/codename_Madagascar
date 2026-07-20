"""Minimal ready-subtask scheduler (M4). No TaskPlan rewriting / slow loop."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from orchestra.backends.base import ArtifactRef, BackendSessionRef
from orchestra.cli.validate_graph import build_compiler
from orchestra.communication.ledger import DeliveryRecord
from orchestra.control.backend_usage import (
    BackendUsageRecord,
    collect_usage_from_graph_result,
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
from orchestra.control.slow_loop.controller import SlowLoopController
from orchestra.control.slow_loop.graph_materializer import FutureGraphMaterializer
from orchestra.control.slow_loop.revision import PlanRevisionCorruption
from orchestra.control.slow_loop.schemas import (
    SlowLoopConfig,
    TaskSchedulingPolicy,
)
from orchestra.control.slow_loop.task_budget import TaskBudgetTracker
from orchestra.control.task_state import (
    BackendSessionRecord,
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


def _harness_command(graph: OrchestraGraph) -> tuple[list[str], float]:
    for node in graph.nodes:
        if node.node_kind is NodeKind.HARNESS:
            command = list(
                getattr(node, "command", None) or ["python", "-m", "pytest", "-q"]
            )
            timeout = float(getattr(node, "timeout_seconds", None) or 60.0)
            return command, timeout
    return ["python", "-m", "pytest", "-q"], 60.0


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
        ready = [
            sid
            for sid, sub in state.subtasks.items()
            if sub.status is SubtaskStatus.READY
            and sub.lease_status != "leased"
            and not sub.communication_block_reason
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
            state.scheduling_policy = TaskSchedulingPolicy(
                max_concurrent_subtasks=self.max_concurrent_subtasks
            )

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
                # Every dependency-ready target is communication-blocked.
                break

            # Acquire leases only for deliverable targets (future plan freeze).
            leased = set(deliverable)
            for sid in leased:
                sub = state.subtasks[sid]
                sub.lease_status = "leased"
                sub.lease_plan_version = state.task_plan.plan_version
                sub.lease_acquired_state_version = state.state_version
            state.state_version += 1
            await self.task_checkpoint_store.save(state)

            # Snapshot for workers (deep copy) so they never mutate shared state.
            state_snapshot = state.model_copy(deep=True)
            concurrency = self._effective_concurrency(state)
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

            # Release leases after wave commits.
            for sid in leased:
                sub = state.subtasks[sid]
                if sub.lease_status == "leased":
                    sub.lease_status = "released"
            await self.task_checkpoint_store.save(state)

            # Slow Loop only at safe checkpoint between waves.
            async with self._state_lock:
                task_budget = self.task_budget_tracker.snapshot(
                    task_plan=state.task_plan,
                    task_state=state,
                )
                await self.slow_loop.maybe_update(
                    task_plan=state.task_plan,
                    state=state,
                    context=context,
                    leased_subtask_ids=set(),
                    task_budget=task_budget,
                )
                await self.task_checkpoint_store.save(state)

            if not self._ready_ids(state):
                break

        state.frozen = all(
            s.status is SubtaskStatus.COMMITTED for s in state.subtasks.values()
        )
        state.state_version += 1
        await self.task_checkpoint_store.save(state)
        return state

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
        attempt_id = len(sub.attempts) or 1
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
            sub.last_commit_record_id = record.record_id
            state.subtasks[sid] = sub
            state.committed_subtask_count += 1
            self._merge_fast_loop(state, result)
            state.clear_communication_blocks()
            state.mark_ready_from_dependencies()
            state.state_version += 1
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
        command, timeout = _harness_command(graph)

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
        state.canonical_workspace_ref = promoted.path
        state.canonical_revision = record.committed_revision
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
        state.clear_communication_blocks()
        state.mark_ready_from_dependencies()
        state.state_version += 1

    def _merge_fast_loop(
        self, state: TaskExecutionState, result: SubtaskExecutionResult
    ) -> None:
        if result.fast_loop_state is not None:
            state.fast_loop_states[result.subtask_id] = result.fast_loop_state
        for item in result.fast_loop_history_append:
            state.fast_loop_history.append(item)
        for rec in result.delivery_records_append:
            state.delivery_ledger.append(rec)
        existing_usage = {r.usage_id for r in (state.backend_usage_records or [])}
        for rec in result.backend_usage_append:
            if rec.usage_id not in existing_usage:
                state.backend_usage_records.append(rec)
                existing_usage.add(rec.usage_id)

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
            )
        )

        run_context = RunContext(
            run_id=f"{context.run_id}:{subtask_id}:{attempt_id}",
            task_id=context.task_id,
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
            sub.status = status
            sub.failure_reason = reason
            sub.failure_message = message
            sub.attempts[-1].status = status
            sub.attempts[-1].finished_at = datetime.now(UTC)
            sub.attempts[-1].error = message
            local_state.subtasks[subtask_id] = sub
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
        local_state.backend_usage_records.extend(usage_records)
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
