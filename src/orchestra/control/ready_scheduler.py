"""Minimal ready-subtask scheduler (M4). No TaskPlan rewriting / slow loop."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from orchestra.backends.base import ArtifactRef, BackendSessionRef
from orchestra.cli.validate_graph import build_compiler
from orchestra.control.canonical_workspace import CanonicalTaskWorkspaceManager
from orchestra.control.failure import classify_subtask_outcome
from orchestra.control.fast_loop.controller import FastLoopController
from orchestra.control.fast_loop.schemas import CostRecord, FastLoopBudget
from orchestra.control.fast_loop.workspace import GitCandidateWorkspaceManager
from orchestra.control.input_assembler import SubtaskInputAssembler
from orchestra.control.task_state import (
    BackendSessionRecord,
    SubtaskAttempt,
    SubtaskFailureReason,
    SubtaskState,
    SubtaskStatus,
    TaskExecutionState,
)
from orchestra.decomposition.schemas import TaskPlan
from orchestra.ir.artifacts import ArtifactBundle
from orchestra.ir.graph import load_graph
from orchestra.runtime.backend import RunContext
from orchestra.runtime.native_async import NativeAsyncRuntime
from orchestra.runtime.state import GraphExecutionResult
from orchestra.runtime.task_checkpoint import TaskCheckpointStore
from orchestra.storage.artifacts import ArtifactStore


class SubtaskStatePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subtask: SubtaskState
    fast_loop_state: Any | None = None
    fast_loop_history_append: list[Any] = Field(default_factory=list)
    canonical_revision: str | None = None
    canonical_workspace_ref: str | None = None


class SubtaskExecutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subtask_id: str
    expected_state_version: int
    state_patch: SubtaskStatePatch


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
    ) -> None:
        if max_concurrent_subtasks > 1 and not allow_concurrent_subtasks:
            # Fail closed: concurrent shared-state mutation is unsafe without
            # the merge path below. Set allow_concurrent_subtasks=True to enable.
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
        )
        self.compiler = build_compiler(contracts_dir)
        self._state_lock = asyncio.Lock()

    def _ready_ids(self, state: TaskExecutionState) -> list[str]:
        state.mark_ready_from_dependencies()
        ready = [
            sid
            for sid, sub in state.subtasks.items()
            if sub.status is SubtaskStatus.READY
        ]
        return sorted(ready)

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

        while True:
            for sid, sub in state.subtasks.items():
                if sub.status is SubtaskStatus.PENDING and self._deps_failed(state, sid):
                    sub.status = SubtaskStatus.SKIPPED
                    sub.failure_message = "blocked by failed dependency"

            ready = self._ready_ids(state)
            if not ready:
                break

            if self.max_concurrent_subtasks <= 1:
                for sid in ready:
                    result = await self._run_subtask_isolated(
                        task_plan=task_plan,
                        state=state,
                        subtask_id=sid,
                        initial_artifacts=initial_artifacts,
                        context=context,
                        source_repo=repo,
                    )
                    await self._merge_result(state, result)
            else:
                expected = state.state_version
                gathered = await asyncio.gather(
                    *[
                        self._run_subtask_isolated(
                            task_plan=task_plan,
                            state=state,
                            subtask_id=sid,
                            initial_artifacts=initial_artifacts,
                            context=context,
                            source_repo=repo,
                            expected_state_version=expected,
                        )
                        for sid in ready
                    ]
                )
                for result in gathered:
                    await self._merge_result(state, result)

            if not self._ready_ids(state):
                break

        state.frozen = all(
            s.status is SubtaskStatus.COMMITTED for s in state.subtasks.values()
        )
        state.state_version += 1
        await self.task_checkpoint_store.save(state)
        return state

    async def _merge_result(
        self, state: TaskExecutionState, result: SubtaskExecutionResult
    ) -> None:
        async with self._state_lock:
            if result.expected_state_version not in {-1, state.state_version}:
                # Soft accept when sequential (-1) or version advanced only by
                # other merges in the same wave — still apply patch fields.
                pass
            sid = result.subtask_id
            state.subtasks[sid] = result.state_patch.subtask
            if result.state_patch.fast_loop_state is not None:
                state.fast_loop_states[sid] = result.state_patch.fast_loop_state
            for item in result.state_patch.fast_loop_history_append:
                state.fast_loop_history.append(item)
            if result.state_patch.canonical_revision:
                state.canonical_revision = result.state_patch.canonical_revision
            if result.state_patch.canonical_workspace_ref:
                state.canonical_workspace_ref = result.state_patch.canonical_workspace_ref
            state.mark_ready_from_dependencies()
            state.state_version += 1
            await self.task_checkpoint_store.save(state)

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
        del task_plan
        # Work on a deep copy of the subtask; do not mutate shared state here.
        sub = state.subtasks[subtask_id].model_copy(deep=True)
        if sub.status is SubtaskStatus.COMMITTED:
            return SubtaskExecutionResult(
                subtask_id=subtask_id,
                expected_state_version=expected_state_version,
                state_patch=SubtaskStatePatch(subtask=sub),
            )

        assembled = await self.input_assembler.assemble(
            task_plan=state.task_plan,
            task_state=state,
            subtask=sub.spec,
            root_artifacts=initial_artifacts,
        )
        sub.applied_dependency_artifact_ids = (
            self.input_assembler.declared_dependency_artifact_ids(
                task_state=state, subtask=sub.spec
            )
        )

        workspace_ref = sub.workspace_ref
        base_task_revision = state.canonical_revision
        if source_repo and state.canonical_workspace_ref:
            from orchestra.workspaces.base import WorkspaceRef

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

        graph = load_graph(sub.spec.local_graph_template)
        compiled = self.compiler.compile(graph)
        attempt_id = len(sub.attempts) + 1
        started = datetime.now(UTC)
        sub.status = SubtaskStatus.RUNNING
        sub.failure_reason = None
        sub.failure_message = None
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
            return self._result_from_local(
                local_state, subtask_id, expected_state_version
            )

        finished = datetime.now(UTC)
        sub.attempts[-1].finished_at = finished
        sub.backend_sessions.extend(
            _collect_backend_sessions(result=result, attempt_id=attempt_id)
        )
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
            sub.committed_artifacts = [
                ArtifactRef(
                    slot=compiled.graph.final_output_slot,
                    artifact_id=final_id,
                    artifact_type=final_artifact.artifact_type,
                )
            ]
            sub.failure_reason = None
            sub.failure_message = None
            sub.attempts[-1].error = None
            # Promote workspace HEAD to task canonical when repo editing succeeded.
            if state.canonical_workspace_ref and workspace_ref:
                await self._promote_workspace_to_canonical(
                    state=local_state,
                    workspace_path=workspace_ref,
                    subtask_id=subtask_id,
                    run_dir=str(context.run_dir),
                )
            local_state.subtasks[subtask_id] = sub
            return self._result_from_local(
                local_state, subtask_id, expected_state_version
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

        # Fast Loop candidates fork from immutable canonical (or source), never
        # from the dirty failed attempt workspace.
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

        # If Fast Loop committed, promote winner base to task canonical.
        sub_after = local_state.subtasks[subtask_id]
        if (
            sub_after.status is SubtaskStatus.COMMITTED
            and local_state.canonical_workspace_ref
            and sub_after.workspace_ref
        ):
            await self._promote_workspace_to_canonical(
                state=local_state,
                workspace_path=sub_after.workspace_ref,
                subtask_id=subtask_id,
                run_dir=str(context.run_dir),
            )

        return self._result_from_local(local_state, subtask_id, expected_state_version)

    def _result_from_local(
        self,
        local_state: TaskExecutionState,
        subtask_id: str,
        expected_state_version: int,
    ) -> SubtaskExecutionResult:
        return SubtaskExecutionResult(
            subtask_id=subtask_id,
            expected_state_version=expected_state_version,
            state_patch=SubtaskStatePatch(
                subtask=local_state.subtasks[subtask_id],
                fast_loop_state=local_state.fast_loop_states.get(subtask_id),
                fast_loop_history_append=[
                    h
                    for h in local_state.fast_loop_history
                    if h.subtask_id == subtask_id
                ],
                canonical_revision=local_state.canonical_revision,
                canonical_workspace_ref=local_state.canonical_workspace_ref,
            ),
        )

    async def _promote_workspace_to_canonical(
        self,
        *,
        state: TaskExecutionState,
        workspace_path: str,
        subtask_id: str,
        run_dir: str,
    ) -> None:
        if not state.canonical_workspace_ref:
            return
        import shutil
        from pathlib import Path

        from orchestra.workspaces.base import WorkspaceRef

        canonical = WorkspaceRef(
            workspace_id=f"{state.task_id}/canonical",
            path=state.canonical_workspace_ref,
            kind="CANONICAL_TASK_WORKSPACE",
            task_id=state.task_id,
            subtask_id="__canonical__",
            base_revision=state.canonical_revision,
        )
        winner = WorkspaceRef(
            workspace_id=f"{state.task_id}/{subtask_id}/promote",
            path=workspace_path,
            kind="SHARED_SUBTASK_WORKSPACE",
            task_id=state.task_id,
            subtask_id=subtask_id,
            base_revision=None,
        )
        try:
            change_set = await self._candidate_ws.collect_changeset(winner)
            has_dirty = bool(
                change_set.modified_files
                or change_set.added_untracked_files
                or change_set.deleted_files
                or change_set.renamed_files
                or change_set.tracked_patch.strip()
            )
            if has_dirty:
                updated = await self.canonical.apply_committed_changeset(
                    canonical=canonical,
                    winner_workspace=winner,
                    change_set=change_set,
                    expected_revision=state.canonical_revision,
                    commit_message=f"promote {subtask_id}",
                )
                state.canonical_revision = updated.base_revision
                state.canonical_workspace_ref = updated.path
            else:
                # Winner already finalized (clean tree). Replace canonical with
                # a fresh clone so downstream sees the committed revision.
                dest = Path(canonical.path)
                src = Path(winner.path)

                def _resync() -> str | None:
                    parent = dest.parent
                    if dest.exists():
                        shutil.rmtree(dest)
                    parent.mkdir(parents=True, exist_ok=True)
                    clone = __import__("subprocess").run(
                        ["git", "clone", "--local", str(src), str(dest)],
                        check=False,
                        capture_output=True,
                        text=True,
                    )
                    if clone.returncode != 0:
                        shutil.copytree(src, dest, symlinks=False)
                    rev = self.canonical._rev_parse(dest)
                    return rev

                new_rev = await asyncio.to_thread(_resync)
                state.canonical_revision = new_rev
                state.canonical_workspace_ref = str(dest.resolve())
            state.subtasks[subtask_id].base_task_revision = state.canonical_revision
        except Exception:  # noqa: BLE001
            sub = state.subtasks[subtask_id]
            if sub.status is SubtaskStatus.COMMITTED:
                sub.failure_message = (
                    (sub.failure_message or "")
                    + " ; canonical promote skipped/conflict"
                ).strip(" ;")
            else:
                sub.status = SubtaskStatus.FAILED
                sub.failure_reason = SubtaskFailureReason.DEPENDENCY_CONFLICT
