"""Single-subtask compatibility runner over NativeAsyncRuntime."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from orchestra.backends.base import ArtifactRef, BackendSessionRef
from orchestra.cli.validate_graph import build_compiler
from orchestra.control.failure import classify_subtask_outcome
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


class SingleSubtaskCompatibilityError(ValueError):
    pass


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


def _workspace_ref_from_metadata(result: GraphExecutionResult) -> str | None:
    for meta in result.state.node_backend_metadata.values():
        workspace = meta.get("workspace_ref")
        if workspace:
            return str(workspace)
    return None


class SingleSubtaskCompatibilityRunner:
    """Execute a one-subtask TaskPlan via the existing graph runtime.

    Multi-subtask scheduling belongs to Milestone 4.
    """

    def __init__(
        self,
        *,
        runtime: NativeAsyncRuntime,
        artifact_store: ArtifactStore,
        task_checkpoint_store: TaskCheckpointStore,
        contracts_dir: str = "configs/contracts",
        workspace_manager: SharedSubtaskGitWorkspaceManager | None = None,
        source_repo: str | None = None,
    ) -> None:
        self.runtime = runtime
        self.artifact_store = artifact_store
        self.task_checkpoint_store = task_checkpoint_store
        self.contracts_dir = contracts_dir
        self.workspace_manager = workspace_manager or SharedSubtaskGitWorkspaceManager()
        self.source_repo = source_repo

    def _require_single(self, plan: TaskPlan) -> str:
        if len(plan.subtasks) != 1:
            raise SingleSubtaskCompatibilityError(
                f"single-subtask mode requires exactly 1 subtask, got {len(plan.subtasks)}"
            )
        return plan.subtasks[0].subtask_id

    async def run(
        self,
        *,
        plan: TaskPlan,
        initial_artifacts: ArtifactBundle,
        context: RunContext,
        state: TaskExecutionState | None = None,
        source_repo: str | None = None,
    ) -> tuple[TaskExecutionState, GraphExecutionResult | None]:
        subtask_id = self._require_single(plan)
        if state is None:
            loaded = await self.task_checkpoint_store.load(
                plan.task_id,
                plan_version=plan.plan_version,
                plan_content_hash=plan.content_hash(),
                allow_config_drift=context.allow_config_drift,
            )
            state = loaded or TaskExecutionState.from_plan(
                plan,
                artifact_store_ref=str(Path(context.run_dir) / "artifacts"),
            )

        sub = state.subtasks[subtask_id]
        if sub.status is SubtaskStatus.COMMITTED and state.frozen:
            return state, None

        workspace_ref = sub.workspace_ref or context.workspace_ref
        repo = source_repo or self.source_repo
        if repo and not workspace_ref:
            prepared = await self.workspace_manager.prepare(
                source_repo=repo,
                run_dir=str(context.run_dir),
                task_id=plan.task_id,
                subtask_id=subtask_id,
            )
            workspace_ref = prepared.path
            sub.workspace_ref = workspace_ref
        elif workspace_ref:
            sub.workspace_ref = workspace_ref

        run_context = RunContext(
            run_id=context.run_id,
            task_id=context.task_id,
            run_dir=context.run_dir,
            limits=context.limits,
            semaphores=context.semaphores,
            contract_hash=context.contract_hash,
            allow_config_drift=context.allow_config_drift,
            subtask_id=subtask_id,
            workspace_ref=workspace_ref,
        )

        graph = load_graph(sub.spec.local_graph_template)
        compiled = build_compiler(self.contracts_dir).compile(graph)
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

        try:
            result = await self.runtime.execute(
                graph=compiled,
                initial_artifacts=initial_artifacts,
                context=run_context,
            )
        except Exception as exc:  # noqa: BLE001 - persist failure then re-raise
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
            state.frozen = False
            await self.task_checkpoint_store.save(state)
            raise

        finished = datetime.now(UTC)
        sub.attempts[-1].finished_at = finished
        sub.backend_sessions.extend(
            _collect_backend_sessions(result=result, attempt_id=attempt_id)
        )
        ws = _workspace_ref_from_metadata(result)
        if ws:
            sub.workspace_ref = ws

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
            state.frozen = True
        else:
            sub.final_output_artifact_id = None
            sub.failure_reason = reason
            sub.failure_message = message
            sub.attempts[-1].error = message
            state.frozen = False

        await self.task_checkpoint_store.save(state)
        return state, result


def summarize_task_state(state: TaskExecutionState) -> dict[str, Any]:
    return {
        "task_id": state.task_id,
        "frozen": state.frozen,
        "plan_version": state.task_plan.plan_version,
        "decomposition_status": state.task_plan.decomposition_status.value,
        "subtasks": {
            sid: {
                "status": sub.status.value,
                "attempts": len(sub.attempts),
                "final_output_artifact_id": sub.final_output_artifact_id,
                "workspace_ref": sub.workspace_ref,
                "failure_reason": (
                    sub.failure_reason.value if sub.failure_reason else None
                ),
                "failure_message": sub.failure_message,
                "backend_sessions": [
                    record.model_dump(mode="json") for record in sub.backend_sessions
                ],
            }
            for sid, sub in state.subtasks.items()
        },
    }
