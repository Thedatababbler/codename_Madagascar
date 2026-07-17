"""Milestone 4 Fast Loop schemas (LocalEdit, candidates, budgets, state)."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from orchestra.backends.base import BackendSessionRef
from orchestra.backends.capabilities import SessionPolicy
from orchestra.control.task_state import BackendSessionRecord, SubtaskFailureReason
from orchestra.ir.graph import OrchestraGraph
from orchestra.workspaces.base import WorkspaceRef


class PromptFeedbackEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["prompt_feedback"] = "prompt_feedback"
    node_id: str
    feedback: str


class ModelOverrideEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["model_override"] = "model_override"
    node_id: str
    model_name: str


class ToolPolicyEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["tool_policy"] = "tool_policy"
    node_id: str
    add_tools: list[str] = Field(default_factory=list)
    remove_tools: list[str] = Field(default_factory=list)


class BudgetAdjustmentEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["budget_adjustment"] = "budget_adjustment"
    node_id: str
    max_steps_delta: int = 0
    timeout_seconds_delta: int = 0


class AddVerifierNodeEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["add_verifier_node"] = "add_verifier_node"
    target_node_id: str
    verifier_template_id: str


class SessionPolicyEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["session_policy"] = "session_policy"
    node_id: str
    policy: SessionPolicy
    parent_session_ref: BackendSessionRef | None = None


LocalEdit = Annotated[
    PromptFeedbackEdit
    | ModelOverrideEdit
    | ToolPolicyEdit
    | BudgetAdjustmentEdit
    | AddVerifierNodeEdit
    | SessionPolicyEdit,
    Field(discriminator="type"),
]


class FailureDiagnosis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: SubtaskFailureReason
    retryable: bool
    evidence_artifact_ids: list[str] = Field(default_factory=list)
    concise_feedback: str
    recommended_edit_types: list[str] = Field(default_factory=list)
    infrastructure_related: bool = False


class CostRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt_tokens: int = 0
    completion_tokens: int = 0
    estimated_cost_usd: float = 0.0
    backend_calls: int = 0


class StabilityIncident(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    message: str
    node_id: str | None = None


class FastLoopBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_candidates: int = 3
    max_total_backend_calls: int = 8
    max_total_cost: float | None = None
    max_total_tokens: int | None = None
    max_wall_time_seconds: int = 600
    max_attempts_per_subtask: int = 4


class LocalCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: str
    parent_graph_hash: str
    edits: list[LocalEdit]
    graph: OrchestraGraph
    session_policy: SessionPolicy = SessionPolicy.FRESH
    generation_reason: str
    compatibility_rejected: bool = False
    rejection_reason: str | None = None


class CandidateCompatibilityResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    compatible: bool
    reason: str | None = None


class CandidateStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    BACKEND_FAILED = "backend_failed"
    HARNESS_FAILED = "harness_failed"
    VALID = "valid"
    REJECTED = "rejected"
    COMMITTED = "committed"
    DISCARDED = "discarded"


class CandidateRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: str
    attempt_id: int
    graph_hash: str
    parent_graph_hash: str
    edits: list[LocalEdit]
    workspace_ref: WorkspaceRef | None = None
    backend_sessions: list[BackendSessionRecord] = Field(default_factory=list)
    output_artifact_ids: list[str] = Field(default_factory=list)
    harness_artifact_id: str | None = None
    status: CandidateStatus = CandidateStatus.PENDING
    quality_score: float | None = None
    cost: CostRecord = Field(default_factory=CostRecord)
    stability_incidents: list[StabilityIncident] = Field(default_factory=list)
    latency_ms: int | None = None
    failure_reason: SubtaskFailureReason | None = None
    failure_message: str | None = None
    session_policy: SessionPolicy = SessionPolicy.FRESH
    patch: str = ""
    changed_files: list[str] = Field(default_factory=list)
    patch_hash: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class FastLoopState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subtask_id: str
    base_attempt_id: int
    base_graph_hash: str
    diagnosis: FailureDiagnosis
    candidates: list[CandidateRecord] = Field(default_factory=list)
    selected_candidate_id: str | None = None
    exhausted: bool = False
    search_cost: CostRecord = Field(default_factory=CostRecord)
    selected_execution_cost: CostRecord = Field(default_factory=CostRecord)
    infra_retries_used: int = 0
