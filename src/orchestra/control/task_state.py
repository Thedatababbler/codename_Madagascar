"""Task and subtask execution state (Milestone 3 / 3.5)."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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


class SubtaskFailureReason(StrEnum):
    HARNESS = "harness"
    MODEL = "model"
    TOOL = "tool"
    OUTPUT_CONTRACT = "output_contract"
    TIMEOUT = "timeout"
    INFRA = "infra"
    INVALID_CONFIG = "invalid_config"
    UNKNOWN = "unknown"


class BackendSessionRecord(BaseModel):
    """Node/attempt-scoped backend session (not keyed by backend_id alone)."""

    model_config = ConfigDict(extra="forbid")

    node_id: str
    backend_id: str
    attempt_id: int
    session_ref: BackendSessionRef
    candidate_id: str | None = None


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


def migrate_backend_sessions(value: Any) -> list[Any]:
    """Migrate legacy dict[str, BackendSessionRef] or reject unknown shapes.

    Legacy dict keys were backend_id strings. Migrated records use
    ``node_id=backend_id`` and ``attempt_id=1`` as an explicit best-effort
    mapping (pre-M3.5-final checkpoints only).
    """
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        records: list[dict[str, Any]] = []
        for key, ref in value.items():
            if isinstance(ref, BackendSessionRef):
                dumped = ref.model_dump(mode="json")
            elif isinstance(ref, dict):
                dumped = ref
            else:
                raise ValueError(
                    f"Cannot migrate backend_sessions entry {key!r}: "
                    f"unsupported type {type(ref).__name__}"
                )
            backend_id = str(dumped.get("backend_id") or key)
            records.append(
                {
                    "node_id": backend_id,
                    "backend_id": backend_id,
                    "attempt_id": 1,
                    "session_ref": dumped,
                    "candidate_id": None,
                }
            )
        return records
    raise ValueError(
        "backend_sessions must be list[BackendSessionRecord] "
        f"(or legacy dict for explicit migration); got {type(value).__name__}"
    )


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
    backend_sessions: list[BackendSessionRecord] = Field(default_factory=list)
    failure_reason: SubtaskFailureReason | None = None
    failure_message: str | None = None

    @field_validator("backend_sessions", mode="before")
    @classmethod
    def _migrate_sessions(cls, value: Any) -> Any:
        return migrate_backend_sessions(value)


class TaskExecutionState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    task_plan: TaskPlan
    subtasks: dict[str, SubtaskState]
    artifact_store_ref: str = ""
    communication_plan: CommunicationPlan = Field(default_factory=CommunicationPlan)
    fast_loop_history: list[LocalUpdateRecord] = Field(default_factory=list)
    slow_loop_history: list[GlobalUpdateRecord] = Field(default_factory=list)
    # M4: per-subtask FastLoopState (typed at runtime; Any avoids circular import).
    fast_loop_states: dict[str, Any] = Field(default_factory=dict)
    global_revision: int = 0
    frozen: bool = False
    plan_content_hash: str = ""

    @model_validator(mode="after")
    def _coerce_fast_loop_states(self) -> TaskExecutionState:
        if not self.fast_loop_states:
            return self
        from orchestra.control.fast_loop.schemas import FastLoopState

        coerced: dict[str, Any] = {}
        for key, value in self.fast_loop_states.items():
            if isinstance(value, FastLoopState):
                coerced[key] = value
            else:
                coerced[key] = FastLoopState.model_validate(value)
        self.fast_loop_states = coerced
        return self

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
