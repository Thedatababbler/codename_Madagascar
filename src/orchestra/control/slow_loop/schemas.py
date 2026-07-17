"""Milestone 5 Slow Loop schemas (observation, edits, revisions, budgets)."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from orchestra.communication.aggregation import AggregationRule
from orchestra.communication.payload import DeliveryRule, PayloadContract
from orchestra.communication.plan import CommunicationPlan
from orchestra.control.fast_loop.schemas import CostRecord
from orchestra.decomposition.schemas import TaskPlan


class SlowLoopTriggerReason(StrEnum):
    PERIODIC_COMMIT_CHECKPOINT = "periodic_commit_checkpoint"
    CONTEXT_PRESSURE = "context_pressure"
    BUDGET_PRESSURE = "budget_pressure"
    REPEATED_BACKEND_FAILURE = "repeated_backend_failure"
    REPEATED_HARNESS_FAILURE = "repeated_harness_failure"
    CANONICAL_CONFLICT = "canonical_conflict"
    DELIVERY_FAILURE = "delivery_failure"
    AGGREGATION_RISK = "aggregation_risk"


class GlobalDiagnosisReason(StrEnum):
    NO_CHANGE = "no_change"
    CONTEXT_PRESSURE = "context_pressure"
    MISSING_PAYLOAD = "missing_payload"
    REDUNDANT_PAYLOAD = "redundant_payload"
    BUDGET_PRESSURE = "budget_pressure"
    BACKEND_INSTABILITY = "backend_instability"
    SCHEDULING_CONTENTION = "scheduling_contention"
    CANONICAL_CONFLICT_RISK = "canonical_conflict_risk"
    AGGREGATION_RISK = "aggregation_risk"


class SubtaskLeaseStatus(StrEnum):
    UNLEASED = "unleased"
    LEASED = "leased"
    RELEASED = "released"


class GlobalPlanRevisionStatus(StrEnum):
    PROPOSED = "proposed"
    VALIDATED = "validated"
    REJECTED = "rejected"
    APPLIED = "applied"
    SUPERSEDED = "superseded"
    FAILED = "failed"


class GlobalCandidateValidationStatus(StrEnum):
    VALID = "valid"
    INVALID = "invalid"
    REJECTED = "rejected"


class TaskBudgetRemaining(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_backend_calls: int | None = None
    remaining_backend_calls: int | None = None
    max_cost_usd: float | None = None
    remaining_cost_usd: float | None = None
    ratio: float = 1.0


class DeliveryStatistics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    delivered_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    unique_payloads: int = 0


class TaskSchedulingPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_concurrent_subtasks: int = 1
    priority_overrides: dict[str, int] = Field(default_factory=dict)
    serialization_groups: list[list[str]] = Field(default_factory=list)
    version: int = 1


class SlowLoopBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_updates_per_task: int = 4
    max_candidates_per_update: int = 3
    max_control_backend_calls: int = 0
    max_control_tokens: int = 0
    max_wall_time_seconds: float = 30.0
    min_commits_between_updates: int = 1
    failure_policy: Literal["keep_previous_plan", "fail_task"] = "keep_previous_plan"
    # Trigger thresholds
    context_pressure_ratio: float = 0.9
    budget_pressure_ratio: float = 0.35
    repeated_failure_threshold: int = 2


class SlowLoopConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Default off so M4 paths are unchanged; M5 tests/smoke enable explicitly.
    enabled: bool = False
    budget: SlowLoopBudget = Field(default_factory=SlowLoopBudget)
    allowed_backend_assignments: dict[str, list[str]] = Field(default_factory=dict)
    backend_model_pools: dict[str, list[str]] = Field(default_factory=dict)


class GlobalObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    state_version: int
    active_plan_version: int
    active_communication_version: int
    committed_subtasks: list[str] = Field(default_factory=list)
    running_subtasks: list[str] = Field(default_factory=list)
    pending_subtasks: list[str] = Field(default_factory=list)
    ready_subtasks: list[str] = Field(default_factory=list)
    failed_subtasks: list[str] = Field(default_factory=list)
    leased_subtasks: list[str] = Field(default_factory=list)
    total_execution_cost: CostRecord = Field(default_factory=CostRecord)
    total_fast_loop_cost: CostRecord = Field(default_factory=CostRecord)
    remaining_task_budget: TaskBudgetRemaining = Field(default_factory=TaskBudgetRemaining)
    recent_failure_counts: dict[str, int] = Field(default_factory=dict)
    backend_failure_counts: dict[str, int] = Field(default_factory=dict)
    backend_latency_summary: dict[str, float] = Field(default_factory=dict)
    canonical_merge_conflicts: int = 0
    harness_failures: int = 0
    artifact_token_estimates: dict[str, int] = Field(default_factory=dict)
    target_context_pressure: dict[str, float] = Field(default_factory=dict)
    delivery_statistics: DeliveryStatistics = Field(default_factory=DeliveryStatistics)
    current_max_concurrency: int = 1
    current_serialization_groups: list[list[str]] = Field(default_factory=list)
    commits_since_last_slow_update: int = 0


class GlobalDiagnosis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reasons: list[GlobalDiagnosisReason] = Field(default_factory=list)
    affected_future_subtask_ids: list[str] = Field(default_factory=list)
    evidence_artifact_ids: list[str] = Field(default_factory=list)
    evidence_state_versions: list[int] = Field(default_factory=list)
    recommended_edit_types: list[str] = Field(default_factory=list)
    concise_explanation: str = ""
    update_required: bool = False


class UpsertPayloadContractEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["upsert_payload_contract"] = "upsert_payload_contract"
    contract: PayloadContract


class RemovePayloadContractEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["remove_payload_contract"] = "remove_payload_contract"
    payload_id: str


class UpsertDeliveryRuleEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["upsert_delivery_rule"] = "upsert_delivery_rule"
    rule: DeliveryRule


class ContextBudgetEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["context_budget"] = "context_budget"
    target_subtask_id: str
    max_tokens: int


class AggregationRuleEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["aggregation_rule"] = "aggregation_rule"
    rule: AggregationRule


class PendingGraphTemplateEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["pending_graph_template"] = "pending_graph_template"
    subtask_id: str
    graph_template_id: str


class PendingBackendAssignmentEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["pending_backend_assignment"] = "pending_backend_assignment"
    subtask_id: str
    node_id: str
    backend_id: str
    model_name: str | None = None


class PendingPriorityEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["pending_priority"] = "pending_priority"
    subtask_id: str
    priority: int


class SchedulingConcurrencyEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["scheduling_concurrency"] = "scheduling_concurrency"
    max_concurrent_subtasks: int


class SerializationGroupEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["serialization_group"] = "serialization_group"
    subtask_ids: list[str]


GlobalEdit = Annotated[
    UpsertPayloadContractEdit
    | RemovePayloadContractEdit
    | UpsertDeliveryRuleEdit
    | ContextBudgetEdit
    | AggregationRuleEdit
    | PendingGraphTemplateEdit
    | PendingBackendAssignmentEdit
    | PendingPriorityEdit
    | SchedulingConcurrencyEdit
    | SerializationGroupEdit,
    Field(discriminator="type"),
]


class FutureGraphRevision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subtask_id: str
    parent_graph_hash: str
    graph_hash: str
    graph_path: str
    edits: list[GlobalEdit] = Field(default_factory=list)


class GlobalPlanRevision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision_id: str
    revision_number: int
    parent_revision_id: str | None = None
    parent_plan_hash: str
    new_plan_hash: str
    parent_communication_hash: str
    new_communication_hash: str
    trigger_reasons: list[SlowLoopTriggerReason] = Field(default_factory=list)
    diagnosis: GlobalDiagnosis = Field(default_factory=GlobalDiagnosis)
    edits: list[GlobalEdit] = Field(default_factory=list)
    eligible_subtask_ids: list[str] = Field(default_factory=list)
    rejected_edit_ids: list[str] = Field(default_factory=list)
    created_at_state_version: int
    applied_at_state_version: int | None = None
    status: GlobalPlanRevisionStatus = GlobalPlanRevisionStatus.PROPOSED
    metadata: dict[str, Any] = Field(default_factory=dict)


class GlobalCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: str
    parent_revision_id: str | None = None
    diagnosis: GlobalDiagnosis
    edits: list[GlobalEdit] = Field(default_factory=list)
    proposed_task_plan: TaskPlan
    proposed_communication_plan: CommunicationPlan
    proposed_scheduling_policy: TaskSchedulingPolicy
    validation_status: GlobalCandidateValidationStatus = (
        GlobalCandidateValidationStatus.VALID
    )
    rejection_reason: str | None = None
    heuristic_score: float | None = None


class SubtaskExecutionLease(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subtask_id: str
    plan_version: int
    acquired_state_version: int
    released_state_version: int | None = None


class SlowLoopState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    updates_applied: int = 0
    last_update_state_version: int | None = None
    last_update_revision_id: str | None = None
    control_plane_cost: CostRecord = Field(default_factory=CostRecord)
    commits_at_last_update: int = 0
    last_observation_hash: str | None = None


class SlowLoopUpdateResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    updated: bool
    revision: GlobalPlanRevision | None = None
    trigger_reasons: list[SlowLoopTriggerReason] = Field(default_factory=list)
    diagnosis: GlobalDiagnosis = Field(default_factory=GlobalDiagnosis)
    message: str = ""
