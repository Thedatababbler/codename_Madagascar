"""Minimal ready-subtask scheduler (M4). No TaskPlan rewriting / slow loop."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from orchestra.backends.base import ArtifactRef, BackendSessionRef
from orchestra.cli.validate_graph import build_compiler
from orchestra.control.failure import classify_subtask_outcome
from orchestra.control.fast_loop.controller import FastLoopController
from orchestra.control.fast_loop.schemas import FastLoopBudget
from orchestra.control.task_state import (
    BackendSessionRecord,
    SubtaskAttempt,
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
from orchestra.workspaces.git_workspace import SharedSubtaskGitWorkspaceManager


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


class ReadySubtaskScheduler:
    """Schedule dependency-ready subtasks; invoke FastLoopController on failure."""

    def __init__(
        self,
        *,
        runtime: NativeAsyncRuntime,
        artifact_store: ArtifactStore,
        task_checkpoint_store: TaskCheckpointStore,
        contracts_dir: str = "configs/contracts",
        workspace_manager: SharedSubtaskGitWorkspaceManager | None = None,
        fast_loop: FastLoopController | None = None,
        source_repo: str | None = None,
        max_concurrent_subtasks: int = 1,
        budget: FastLoopBudget | None = None,
    ) -> None:
        self.runtime = runtime
        self.artifact_store = artifact_store
        self.task_checkpoint_store = task_checkpoint_store
        self.contracts_dir = contracts_dir
        self.workspace_manager = workspace_manager or SharedSubtaskGitWorkspaceManager()
        self.source_repo = source_repo
        self.max_concurrent_subtasks = max_concurrent_subtasks
        self.fast_loop = fast_loop or FastLoopController(
            runtime=runtime,
            artifact_store=artifact_store,
            task_checkpoint_store=task_checkpoint_store,
            contracts_dir=contracts_dir,
            budget=budget,
        )
        self.compiler = build_compiler(contracts_dir)

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
        sem = asyncio.Semaphore(self.max_concurrent_subtasks)

        async def _run_one(subtask_id: str) -> None:
            async with sem:
                await self._run_subtask(
                    task_plan=task_plan,
                    state=state,
                    subtask_id=subtask_id,
                    initial_artifacts=initial_artifacts,
                    context=context,
                    source_repo=repo,
                )

        # Deterministic waves: repeatedly schedule current ready set.
        while True:
            # Block downstream of failed deps.
            for sid, sub in state.subtasks.items():
                if sub.status is SubtaskStatus.PENDING and self._deps_failed(state, sid):
                    sub.status = SubtaskStatus.SKIPPED
                    sub.failure_message = "blocked by failed dependency"

            ready = self._ready_ids(state)
            if not ready:
                break
            # Sequential within wave for deterministic checkpoint ordering when
            # concurrency is 1; gather when >1 but still sorted ready ids.
            if self.max_concurrent_subtasks <= 1:
                for sid in ready:
                    await _run_one(sid)
            else:
                await asyncio.gather(*[_run_one(sid) for sid in ready])

            await self.task_checkpoint_store.save(state)
            # Progress check: if nothing committed/failed since last wave and no ready, stop.
            if not self._ready_ids(state):
                break

        state.frozen = all(
            s.status is SubtaskStatus.COMMITTED for s in state.subtasks.values()
        )
        await self.task_checkpoint_store.save(state)
        return state

    async def _run_subtask(
        self,
        *,
        task_plan: TaskPlan,
        state: TaskExecutionState,
        subtask_id: str,
        initial_artifacts: ArtifactBundle,
        context: RunContext,
        source_repo: str | None,
    ) -> None:
        del task_plan  # TaskPlan structure is never modified (M4 boundary).
        sub = state.subtasks[subtask_id]
        if sub.status is SubtaskStatus.COMMITTED:
            return

        workspace_ref = sub.workspace_ref or context.workspace_ref
        if source_repo and not workspace_ref:
            prepared = await self.workspace_manager.prepare(
                source_repo=source_repo,
                run_dir=str(context.run_dir),
                task_id=state.task_id,
                subtask_id=subtask_id,
            )
            workspace_ref = prepared.path
            sub.workspace_ref = workspace_ref

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
        await self.task_checkpoint_store.save(state)

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

        try:
            result = await self.runtime.execute(
                graph=compiled,
                initial_artifacts=initial_artifacts,
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
            await self.task_checkpoint_store.save(state)
            # Infra/model failures may still enter fast loop via controller.
            await self.fast_loop.run(
                state=state,
                subtask_id=subtask_id,
                context=context,
                initial_artifacts=initial_artifacts,
                source_repo=source_repo,
                graph=graph,
            )
            return

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
            state.mark_ready_from_dependencies()
            await self.task_checkpoint_store.save(state)
            return

        sub.failure_reason = reason
        sub.failure_message = message
        sub.attempts[-1].error = message
        sub.status = (
            SubtaskStatus.RETRY_PENDING
            if status is SubtaskStatus.HARNESS_FAILED
            else status
        )
        await self.task_checkpoint_store.save(state)

        # Fast local adaptation (does not rewrite TaskPlan).
        await self.fast_loop.run(
            state=state,
            subtask_id=subtask_id,
            context=context,
            initial_artifacts=initial_artifacts,
            source_repo=source_repo,
            graph=graph,
        )
