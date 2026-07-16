"""Task and subtask execution state (Milestone 3)."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from orchestra.backends.base import ArtifactRef, BackendSessionRef
from orchestra.communication.plan import CommunicationPlan
from orchestra.decomposition.schemas import SubtaskSpec, TaskPlan


class SubtaskStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    HARNESS_FAILED = "harness_failed"
    RETRY_PENDING = "retry_pending"
    COMMITTED = "committed"
    FAILED = "failed"
    SKIPPED = "skipped"


class SubtaskAttempt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attempt_id: int
    status: SubtaskStatus
    started_at: datetime | None = None
    finished_at: datetime | None = None
    graph_hash: str | None = None
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class LocalUpdateRecord(BaseModel):
    """Stub for FastLoop history (Milestone 4)."""

    model_config = ConfigDict(extra="forbid")

    record_id: str
    subtask_id: str
    revision: int
    summary: str = ""
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] = Field(default_factory=dict)


class GlobalUpdateRecord(BaseModel):
    """Stub for SlowLoop history (Milestone 5)."""

    model_config = ConfigDict(extra="forbid")

    record_id: str
    revision: int
    summary: str = ""
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] = Field(default_factory=dict)


class SubtaskState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    spec: SubtaskSpec
    status: SubtaskStatus
    attempts: list[SubtaskAttempt] = Field(default_factory=list)
    committed_artifacts: list[ArtifactRef] = Field(default_factory=list)
    current_graph_hash: str = ""
    local_revision: int = 0
    final_output_artifact_id: str | None = None
    workspace_ref: str | None = None
    backend_sessions: dict[str, BackendSessionRef] = Field(default_factory=dict)


class TaskExecutionState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    task_plan: TaskPlan
    subtasks: dict[str, SubtaskState]
    artifact_store_ref: str = ""
    communication_plan: CommunicationPlan = Field(default_factory=CommunicationPlan)
    fast_loop_history: list[LocalUpdateRecord] = Field(default_factory=list)
    slow_loop_history: list[GlobalUpdateRecord] = Field(default_factory=list)
    global_revision: int = 0
    frozen: bool = False
    plan_content_hash: str = ""

    @classmethod
    def from_plan(
        cls,
        plan: TaskPlan,
        *,
        artifact_store_ref: str = "",
    ) -> TaskExecutionState:
        subtasks: dict[str, SubtaskState] = {}
        for spec in plan.subtasks:
            status = (
                SubtaskStatus.READY
                if not spec.dependencies
                else SubtaskStatus.PENDING
            )
            subtasks[spec.subtask_id] = SubtaskState(spec=spec, status=status)
        return cls(
            task_id=plan.task_id,
            task_plan=plan,
            subtasks=subtasks,
            artifact_store_ref=artifact_store_ref,
            communication_plan=plan.communication_plan.model_copy(deep=True),
            plan_content_hash=plan.content_hash(),
        )

    def mark_ready_from_dependencies(self) -> None:
        committed = {
            sid
            for sid, state in self.subtasks.items()
            if state.status is SubtaskStatus.COMMITTED
        }
        for _sid, state in self.subtasks.items():
            if state.status is not SubtaskStatus.PENDING:
                continue
            deps = state.spec.dependencies
            if all(dep in committed for dep in deps):
                state.status = SubtaskStatus.READY
