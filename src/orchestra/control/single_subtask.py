"""Single-subtask compatibility runner over NativeAsyncRuntime."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from orchestra.backends.base import ArtifactRef
from orchestra.cli.validate_graph import build_compiler
from orchestra.control.task_state import (
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


class SingleSubtaskCompatibilityError(ValueError):
    pass


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
    ) -> None:
        self.runtime = runtime
        self.artifact_store = artifact_store
        self.task_checkpoint_store = task_checkpoint_store
        self.contracts_dir = contracts_dir

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

        graph = load_graph(sub.spec.local_graph_template)
        compiled = build_compiler(self.contracts_dir).compile(graph)
        attempt_id = len(sub.attempts) + 1
        started = datetime.now(UTC)
        sub.status = SubtaskStatus.RUNNING
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
                context=context,
            )
        except Exception as exc:  # noqa: BLE001 - persist failure then re-raise
            sub.status = SubtaskStatus.FAILED
            sub.attempts[-1].status = SubtaskStatus.FAILED
            sub.attempts[-1].finished_at = datetime.now(UTC)
            sub.attempts[-1].error = f"{type(exc).__name__}: {exc}"
            await self.task_checkpoint_store.save(state)
            raise

        finished = datetime.now(UTC)
        sub.attempts[-1].finished_at = finished
        if result.state.frozen and result.state.final_output_artifact_id:
            final_id = result.state.final_output_artifact_id
            final_artifact = await self.artifact_store.get(final_id)
            sub.status = SubtaskStatus.COMMITTED
            sub.attempts[-1].status = SubtaskStatus.COMMITTED
            sub.final_output_artifact_id = final_id
            sub.committed_artifacts = [
                ArtifactRef(
                    slot=compiled.graph.final_output_slot,
                    artifact_id=final_id,
                    artifact_type=final_artifact.artifact_type,
                )
            ]
            state.frozen = True
        else:
            sub.status = SubtaskStatus.FAILED
            sub.attempts[-1].status = SubtaskStatus.FAILED
            sub.attempts[-1].error = "graph execution did not freeze with final output"
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
            }
            for sid, sub in state.subtasks.items()
        },
    }
