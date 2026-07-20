"""Milestone 4 Fast Loop schemas (LocalEdit, candidates, budgets, state)."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

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
    failed_node_ids: list[str] = Field(default_factory=list)
    primary_failed_node_id: str | None = None
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


class CodexSessionMode(StrEnum):
    """Fast Loop Codex session orchestration mode."""

    FRESH_ONLY = "fresh_only"
    RESUME_ONLY = "resume_only"
    FORK_ONLY = "fork_only"
    HYBRID = "hybrid"


class MissingParentPolicy(StrEnum):
    REJECT = "reject"
    FRESH_FALLBACK = "fresh_fallback"


class WorkspaceIncompatibilityPolicy(StrEnum):
    REJECT = "reject"
    FRESH_FALLBACK = "fresh_fallback"


class HybridCodexConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enable_resume: bool = True
    enable_fork: bool = True
    max_resume_candidates: int = 1
    max_fork_candidates: int = 1
    max_fresh_independent_candidates: int = 1
    resume_same_strategy: bool = True
    fork_alternative_strategy: bool = True
    add_fresh_critic: bool = True
    parent_preference: list[str] = Field(
        default_factory=lambda: [
            "failed_initial_attempt",
            "last_selected_candidate",
        ]
    )
    missing_parent_policy: MissingParentPolicy = MissingParentPolicy.REJECT
    workspace_incompatibility_policy: WorkspaceIncompatibilityPolicy = (
        WorkspaceIncompatibilityPolicy.REJECT
    )


class FastLoopConfig(BaseModel):
    """Optional Fast Loop control-plane config (defaults preserve FRESH-only)."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    codex_session_mode: CodexSessionMode = CodexSessionMode.FRESH_ONLY
    hybrid_codex: HybridCodexConfig = Field(default_factory=HybridCodexConfig)
    budget: FastLoopBudget = Field(default_factory=FastLoopBudget)


class NodeSessionDirective(BaseModel):
    """Per-node session lifecycle directive inside a candidate graph."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: str
    backend_id: str
    policy: SessionPolicy
    source_session_ref: BackendSessionRef | None = None
    source_candidate_id: str | None = None
    source_node_id: str | None = None
    source_attempt_id: int | None = None
    workspace_binding: str = "candidate_isolated"
    lineage_reason: str = ""
    require_parent_session: bool = False


class BackendModelPool(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    backend_id: str
    allowed_models: list[str] = Field(default_factory=list)
    fallback_order: list[str] = Field(default_factory=list)


class CandidateRejectionReason(StrEnum):
    UNSUPPORTED_SESSION_POLICY = "unsupported_session_policy"
    UNSUPPORTED_MODEL_OVERRIDE = "unsupported_model_override"
    UNSUPPORTED_TOOL_EDIT = "unsupported_tool_edit"
    INVALID_GRAPH_EDIT = "invalid_graph_edit"
    BUDGET_EXCEEDED = "budget_exceeded"
    WORKSPACE_INCOMPATIBLE = "workspace_incompatible"
    MISSING_GRAPH = "missing_graph"
    OTHER = "other"


class RenameRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    from_path: str
    to_path: str


class WorkspaceChangeSet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tracked_patch: str = ""
    modified_files: list[str] = Field(default_factory=list)
    added_untracked_files: list[str] = Field(default_factory=list)
    deleted_files: list[str] = Field(default_factory=list)
    renamed_files: list[RenameRecord] = Field(default_factory=list)
    file_manifest_hash: str = ""


class LocalCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: str
    parent_graph_hash: str
    edits: list[LocalEdit]
    graph: OrchestraGraph
    # Legacy candidate-level policy (checkpoint migration / audit only).
    # New execution uses session_directives per node.
    session_policy: SessionPolicy = SessionPolicy.FRESH
    session_directives: dict[str, NodeSessionDirective] = Field(default_factory=dict)
    generation_reason: str
    compatibility_rejected: bool = False
    rejection_reason: CandidateRejectionReason | None = None
    rejection_message: str | None = None


class CandidateCompatibilityResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    compatible: bool
    reason: str | None = None
    rejection_reason: CandidateRejectionReason | None = None


class CandidateStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    BACKEND_FAILED = "backend_failed"
    HARNESS_FAILED = "harness_failed"
    VALID = "valid"
    REJECTED = "rejected"
    COMMIT_VALIDATION_FAILED = "commit_validation_failed"
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
    rejection_reason: CandidateRejectionReason | None = None
    rejection_message: str | None = None
    # Legacy candidate-level policy retained for checkpoint migration.
    session_policy: SessionPolicy = SessionPolicy.FRESH
    session_directives: dict[str, NodeSessionDirective] = Field(default_factory=dict)
    patch: str = ""
    changed_files: list[str] = Field(default_factory=list)
    patch_hash: str | None = None
    change_set: WorkspaceChangeSet | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


def sum_candidate_costs(candidates: list[CandidateRecord]) -> CostRecord:
    """Sum execution costs of candidates that actually ran a backend."""
    total = CostRecord()
    for cand in candidates:
        if cand.status is CandidateStatus.REJECTED and cand.cost.backend_calls == 0:
            continue
        total = CostRecord(
            prompt_tokens=total.prompt_tokens + cand.cost.prompt_tokens,
            completion_tokens=total.completion_tokens + cand.cost.completion_tokens,
            estimated_cost_usd=total.estimated_cost_usd + cand.cost.estimated_cost_usd,
            backend_calls=total.backend_calls + cand.cost.backend_calls,
        )
    return total


class FastLoopState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subtask_id: str
    base_attempt_id: int
    base_graph_hash: str
    diagnosis: FailureDiagnosis
    candidates: list[CandidateRecord] = Field(default_factory=list)
    selected_candidate_id: str | None = None
    exhausted: bool = False
    # Control-plane-only cost (e.g. future LLM generators). Deterministic gen = 0.
    control_plane_cost: CostRecord = Field(default_factory=CostRecord)
    # Cost of the initial subtask attempt that triggered the Fast Loop.
    initial_execution_cost: CostRecord = Field(default_factory=CostRecord)
    selected_execution_cost: CostRecord = Field(default_factory=CostRecord)
    infra_retries_used: int = 0
    started_monotonic: float | None = None

    @model_validator(mode="before")
    @classmethod
    def _drop_derived_cost_fields(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data = dict(data)
            # Derived fields may appear in older checkpoints; never load as storage.
            data.pop("search_cost", None)
            data.pop("total_method_cost", None)
            # Migrate accidental double-count field into control_plane if needed.
            if "control_plane_cost" not in data and "search_cost" in data:
                pass
        return data

    @computed_field  # type: ignore[prop-decorator]
    @property
    def search_cost(self) -> CostRecord:
        """Derived: sum of candidate execution costs (not double-counted)."""
        return sum_candidate_costs(self.candidates)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_method_cost(self) -> CostRecord:
        base = self.initial_execution_cost
        search = self.search_cost
        ctrl = self.control_plane_cost
        return CostRecord(
            prompt_tokens=base.prompt_tokens + search.prompt_tokens + ctrl.prompt_tokens,
            completion_tokens=(
                base.completion_tokens + search.completion_tokens + ctrl.completion_tokens
            ),
            estimated_cost_usd=(
                base.estimated_cost_usd
                + search.estimated_cost_usd
                + ctrl.estimated_cost_usd
            ),
            backend_calls=base.backend_calls + search.backend_calls + ctrl.backend_calls,
        )
