"""Typed, backend-neutral data contracts for Pareto global planning."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ObjectiveDirection(StrEnum):
    MAXIMIZE = "maximize"
    MINIMIZE = "minimize"


class ObjectiveSource(StrEnum):
    ESTIMATED = "estimated"
    REALIZED = "realized"
    DECLARED_BUDGET = "declared_budget"
    CONFIGURED_PROFILE = "configured_profile"
    ARCHIVE = "archive"
    HISTORY = "history"
    UNAVAILABLE = "unavailable"


class ParetoEvaluationKind(StrEnum):
    ESTIMATED = "estimated"
    REALIZED = "realized"

class ParetoSelectionStatus(StrEnum):
    SELECTED_COMPLETE_FRONTIER = "selected_complete_frontier"
    SELECTED_PARTIAL_FOR_DATA_COLLECTION = "selected_partial_for_data_collection"
    NO_COMPARABLE_CANDIDATE = "no_comparable_candidate"
    FALLBACK_RULE_BASED = "fallback_rule_based"


class EvaluationVisibility(StrEnum):
    PUBLIC = "public"
    DEVELOPMENT = "development"
    HIDDEN = "hidden"
    PRIVATE = "private"


class ObjectiveValue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: float | None = None
    source: ObjectiveSource = ObjectiveSource.UNAVAILABLE
    available: bool = False
    evaluation_visibility: EvaluationVisibility = EvaluationVisibility.PUBLIC
    evidence_count: int = 0
    detail: str | None = None

    @classmethod
    def unavailable(cls, detail: str | None = None) -> ObjectiveValue:
        return cls(detail=detail)


class ParetoObjectiveVector(BaseModel):
    model_config = ConfigDict(extra="forbid")

    values: dict[str, ObjectiveValue] = Field(default_factory=dict)
    evaluation_kind: ParetoEvaluationKind = ParetoEvaluationKind.ESTIMATED


class CandidateObjectiveEstimate(BaseModel):
    """Candidate-conditioned estimate with evidence and conservative bounds."""

    model_config = ConfigDict(extra="forbid")

    objective_vector: ParetoObjectiveVector
    uncertainty: dict[str, float] = Field(default_factory=dict)
    lower_bounds: dict[str, float] = Field(default_factory=dict)
    upper_bounds: dict[str, float] = Field(default_factory=dict)
    evidence_counts: dict[str, int] = Field(default_factory=dict)
    estimator_version: str = "m6.1"


class CommittedSubtaskFingerprint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subtask_id: str
    spec_hash: str = ""
    committed_revision: str | None = None
    artifact_hashes: list[str] = Field(default_factory=list)


class PublicEvaluationRecord(BaseModel):
    """Only PUBLIC/DEVELOPMENT records are admissible for online quality."""

    model_config = ConfigDict(extra="forbid")

    evaluation_id: str = ""
    run_id: str = ""
    task_id: str = ""
    subtask_id: str | None = None
    decision_id: str | None = None
    harness_id: str = ""
    evaluator_id: str = ""
    evaluator_version: str = "public-harness-v1"
    visibility: EvaluationVisibility = EvaluationVisibility.PUBLIC
    passed: bool = False
    passed_checks: int | None = None
    total_checks: int | None = None
    normalized_score: float = 0.0
    # Legacy alias retained for estimators that still read ``quality``.
    quality: float | None = None
    metric_name: str = "normalized_score"
    metric_value: float | None = None
    availability: str = "available"
    provenance: str = "public_harness_commit"
    source_artifact_hash: str | None = None
    plan_revision: str | None = None
    candidate_content_hash: str | None = None
    edit_signature: str | None = None
    state_version: int = 0
    revision_id: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] = Field(default_factory=dict)

    def resolved_quality(self) -> float:
        if self.availability == "unavailable":
            return 0.0
        if self.metric_value is not None:
            return float(self.metric_value)
        if self.quality is not None:
            return float(self.quality)
        return float(self.normalized_score)


class RawFailureCounts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backend_failures: int = 0
    harness_failures: int = 0
    delivery_failures: int = 0
    aggregation_failures: int = 0
    canonical_conflicts: int = 0
    timeouts: int = 0

    @property
    def total(self) -> int:
        return sum(self.model_dump().values())


class PreferenceProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_id: str = "balanced_knee"
    minimum_quality: float | None = None
    maximum_cost_usd: float | None = None
    maximum_latency_seconds: float | None = None
    maximum_failure_risk: float | None = None
    selection_policy: str = "reference_point"
    objective_weights: dict[str, float] = Field(default_factory=dict)
    caps: dict[str, float] = Field(default_factory=dict)
    reference_point: dict[str, float] = Field(default_factory=dict)
    exploration_budget: int = 0
    seed: int = 42
    allow_partial_objectives: bool = False


class ParetoDecisionContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context_id: str
    parent_plan_hash: str
    parent_communication_hash: str
    committed_prefix: list[str] = Field(default_factory=list)
    eligible_future_subtask_ids: list[str] = Field(default_factory=list)
    triggers: list[str] = Field(default_factory=list)
    diagnosis: dict[str, Any] = Field(default_factory=dict)
    preference_profile_id: str = "balanced_knee"
    backend_capability_hash: str = ""
    task_id: str = ""
    repository_fingerprint: str = ""
    canonical_revision: str | None = None
    parent_revision_id: str | None = None
    committed_prefix_hash: str = ""
    committed_subtasks: list[CommittedSubtaskFingerprint] = Field(default_factory=list)
    objective_config_hash: str = ""
    preference_profile_hash: str = ""
    pricing_version: str = ""
    trigger_reasons: list[str] = Field(default_factory=list)
    diagnosis_reasons: list[str] = Field(default_factory=list)


class ParetoOrchestraCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: str
    content_hash: str
    edit_signature: str
    context_id: str
    edits: list[Any] = Field(default_factory=list)
    global_candidate: Any
    objectives: ParetoObjectiveVector = Field(default_factory=ParetoObjectiveVector)
    raw_failure_counts: RawFailureCounts = Field(default_factory=RawFailureCounts)
    communication_overhead: float = 0.0
    validation_errors: list[str] = Field(default_factory=list)


class RealizationStatus(StrEnum):
    PENDING = "pending"
    REALIZED = "realized"
    CENSORED_NO_ELIGIBLE_WAVE = "censored_no_eligible_wave"
    FAILED = "failed"


class ParetoDecisionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision_id: str
    context: ParetoDecisionContext
    selected_content_hash: str | None = None
    candidate_hashes: list[str] = Field(default_factory=list)
    profile_id: str
    evaluation_kind: ParetoEvaluationKind = ParetoEvaluationKind.ESTIMATED
    reason: str = ""
    activated_revision_id: str | None = None
    activated_state_version: int | None = None
    baseline_usage_index: int = 0
    baseline_delivery_index: int = 0
    baseline_commit_index: int = 0
    baseline_evidence_keys: list[str] = Field(default_factory=list)
    baseline_public_evaluation_index: int = 0
    started_at: datetime | None = None
    completed_at: datetime | None = None
    completed_state_version: int | None = None
    selected_candidate_snapshot: ParetoOrchestraCandidate | None = None
    selection_status: ParetoSelectionStatus = ParetoSelectionStatus.NO_COMPARABLE_CANDIDATE
    # Behavioral realization gates (activation alone is insufficient).
    affected_subtask_ids: list[str] = Field(default_factory=list)
    affected_wave_id: str | None = None
    realization_status: RealizationStatus = RealizationStatus.PENDING
    realization_id: str | None = None
    realization_evidence: dict[str, Any] = Field(default_factory=dict)


class ParetoSelectionProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    selected_global_candidate: Any | None = None
    selected_pareto_candidate: ParetoOrchestraCandidate | None = None
    projected_pareto_state: ParetoSearchState
    decision_record: ParetoDecisionRecord
    selection_status: ParetoSelectionStatus
    context: ParetoDecisionContext


class ParetoSearchState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = 1
    enabled: bool = False
    active_preference_profile: PreferenceProfile | None = None
    pending_decision: ParetoDecisionRecord | None = None
    pending_candidate_hash: str | None = None
    decision_history: list[ParetoDecisionRecord] = Field(default_factory=list)
    archive_snapshot_ref: str | None = None
    estimated_archive_size: int = 0
    realized_archive_size: int = 0
    horizon_usage_index: int = 0
    horizon_delivery_index: int = 0
    horizon_commit_index: int = 0
    decisions: int = 0
    random_seed: int = 42


class ParetoConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    max_candidates: int = 8
    max_estimated_archive_size: int = 64
    max_realized_archive_size: int = 64
    allow_two_edit_pairs: bool = True
    allow_archive_replay: bool = True
    scalarize_without_pareto_filter: bool = False
    fallback_to_rule_based: bool = False
    horizon_commits: int = 1
    epsilon: dict[str, float] = Field(default_factory=dict)
    risk_coefficients: dict[str, float] = Field(
        default_factory=lambda: {
            "backend_failures": 1.0,
            "harness_failures": 1.0,
            "delivery_failures": 1.0,
            "aggregation_failures": 1.0,
            "canonical_conflicts": 1.0,
            "timeouts": 1.0,
        }
    )
    objectives: dict[str, ObjectiveDirection] = Field(
        default_factory=lambda: {
            "quality": ObjectiveDirection.MAXIMIZE,
            "cost": ObjectiveDirection.MINIMIZE,
            "latency": ObjectiveDirection.MINIMIZE,
            "risk": ObjectiveDirection.MINIMIZE,
            "communication_overhead": ObjectiveDirection.MINIMIZE,
        }
    )
