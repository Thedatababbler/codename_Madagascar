"""Typed, backend-neutral data contracts for Pareto global planning."""

from __future__ import annotations

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
    ARCHIVE = "archive"
    HISTORY = "history"
    UNAVAILABLE = "unavailable"


class ParetoEvaluationKind(StrEnum):
    ESTIMATED = "estimated"
    REALIZED = "realized"


class ObjectiveValue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: float | None = None
    source: ObjectiveSource = ObjectiveSource.UNAVAILABLE
    available: bool = False
    evaluation_visibility: ParetoEvaluationKind = ParetoEvaluationKind.ESTIMATED
    evidence_count: int = 0
    detail: str | None = None

    @classmethod
    def unavailable(cls, detail: str | None = None) -> ObjectiveValue:
        return cls(detail=detail)


class ParetoObjectiveVector(BaseModel):
    model_config = ConfigDict(extra="forbid")

    values: dict[str, ObjectiveValue] = Field(default_factory=dict)
    evaluation_kind: ParetoEvaluationKind = ParetoEvaluationKind.ESTIMATED


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


class ParetoDecisionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision_id: str
    context: ParetoDecisionContext
    selected_content_hash: str | None = None
    candidate_hashes: list[str] = Field(default_factory=list)
    profile_id: str
    evaluation_kind: ParetoEvaluationKind = ParetoEvaluationKind.ESTIMATED
    reason: str = ""


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
