"""Decision-horizon telemetry extraction from public state ledgers."""

from __future__ import annotations

from orchestra.control.backend_usage import BackendUsageRecord
from orchestra.control.pareto.objectives import objective_vector_from_usage
from orchestra.control.pareto.schemas import (
    EvaluationVisibility,
    ObjectiveSource,
    ObjectiveValue,
    ParetoConfig,
    RawFailureCounts,
)
from orchestra.control.task_state import TaskExecutionState

_SUCCESS = {"ok", "success", "completed"}
_BACKEND_FAILURE = {
    "model_failure",
    "backend_failure",
    "error",
    "failed",
    "provider_error",
}
_TIMEOUT = {"timeout", "timed_out", "deadline_exceeded"}
_HARNESS = {"harness_failure", "harness_failed", "check_failed"}


def horizon_usage_records(state: TaskExecutionState, start_index: int) -> list[BackendUsageRecord]:
    return [
        r if isinstance(r, BackendUsageRecord) else BackendUsageRecord.model_validate(r)
        for r in (state.backend_usage_records or [])[start_index:]
    ]


def raw_failure_counts(
    state: TaskExecutionState,
    *,
    usage_start: int = 0,
    delivery_start: int = 0,
    commit_start: int = 0,
    public_evaluation_start: int = 0,
) -> RawFailureCounts:
    usage = list(state.backend_usage_records or [])[usage_start:]
    delivery = list(state.delivery_ledger or [])[delivery_start:]
    commits = list(state.workspace_commit_records or [])[commit_start:]
    evaluations = list(getattr(state, "public_evaluation_records", []) or [])[
        public_evaluation_start:
    ]

    def _status(rec) -> str:
        return str(getattr(rec, "status", "")).lower()

    def _failure_reason(rec) -> str:
        reason = getattr(rec, "failure_reason", None)
        return str(getattr(reason, "value", reason) or "").lower()

    backend_failures = sum(1 for r in usage if _status(r) in _BACKEND_FAILURE)
    timeouts = sum(1 for r in usage if _status(r) in _TIMEOUT)
    harness_failures = sum(1 for r in usage if _status(r) in _HARNESS)
    harness_failures += sum(
        1
        for e in evaluations
        if str(getattr(e, "visibility", "")).lower()
        in {EvaluationVisibility.PUBLIC.value, EvaluationVisibility.DEVELOPMENT.value}
        and getattr(e, "passed", None) is False
    )
    delivery_failures = sum(1 for r in delivery if _status(r) in {"failed", "error"})
    aggregation_failures = sum(
        1
        for r in delivery
        if "aggregation" in _failure_reason(r)
        or _status(r) == "failed"
        and "aggregation" in str(getattr(r, "metadata", {})).lower()
    )
    # Non-failure skips and target-not-deliverable audits are not delivery failures.
    canonical_conflicts = sum(
        1 for r in commits if str(getattr(r, "status", "")).lower() == "conflicted"
    )
    return RawFailureCounts(
        backend_failures=backend_failures,
        harness_failures=harness_failures,
        delivery_failures=delivery_failures,
        aggregation_failures=aggregation_failures,
        canonical_conflicts=canonical_conflicts,
        timeouts=timeouts,
    )


def realized_horizon_objectives(
    state: TaskExecutionState,
    *,
    usage_start: int,
    delivery_start: int,
    commit_start: int,
    config: ParetoConfig,
    started_at=None,
    completed_at=None,
    public_evaluation_start: int = 0,
):
    records = horizon_usage_records(state, usage_start)
    failures = raw_failure_counts(
        state,
        usage_start=usage_start,
        delivery_start=delivery_start,
        commit_start=commit_start,
        public_evaluation_start=public_evaluation_start,
    )
    vector = objective_vector_from_usage(
        records,
        raw_failures=failures,
        risk_coefficients=config.risk_coefficients,
        delivery_count=len((state.delivery_ledger or [])[delivery_start:]),
    )
    deliveries = (state.delivery_ledger or [])[delivery_start:]
    communication = 0.0
    token_complete = True
    for d in deliveries:
        tokens = getattr(d, "projected_token_count", None)
        if tokens is None:
            tokens = getattr(d, "estimated_tokens", None)
        if tokens is None:
            token_complete = False
            continue
        communication += float(tokens)
    vector.values["communication_overhead"] = ObjectiveValue(
        value=communication if token_complete else None,
        available=token_complete,
        source=ObjectiveSource.REALIZED,
        evaluation_visibility=EvaluationVisibility.PUBLIC,
        evidence_count=len(deliveries),
        detail="actual delivered projected tokens",
    )
    if started_at is not None and completed_at is not None:
        wall = max(0.0, (completed_at - started_at).total_seconds())
        vector.values["latency"] = ObjectiveValue(
            value=wall,
            available=True,
            source=ObjectiveSource.REALIZED,
            evaluation_visibility=EvaluationVisibility.PUBLIC,
            evidence_count=1,
            detail="wall_latency",
        )
        vector.values["wall_latency"] = ObjectiveValue(
            value=wall,
            available=True,
            source=ObjectiveSource.REALIZED,
            evaluation_visibility=EvaluationVisibility.PUBLIC,
            evidence_count=1,
        )
        node_sum = vector.values.get("sum_node_latency")
        if node_sum and node_sum.available and node_sum.value and wall > 0:
            vector.values["concurrency_speedup"] = ObjectiveValue(
                value=float(node_sum.value) / wall,
                available=True,
                source=ObjectiveSource.REALIZED,
                evaluation_visibility=EvaluationVisibility.PUBLIC,
            )
    evaluations = (getattr(state, "public_evaluation_records", []) or [])[
        public_evaluation_start:
    ]
    quality = []
    for e in evaluations:
        visibility = str(
            getattr(e, "visibility", None) if not isinstance(e, dict) else e.get("visibility", "")
        ).lower()
        if visibility not in {
            EvaluationVisibility.PUBLIC.value,
            EvaluationVisibility.DEVELOPMENT.value,
        }:
            continue
        score = (
            getattr(e, "normalized_score", None)
            if not isinstance(e, dict)
            else e.get("normalized_score")
        )
        if score is None:
            score = getattr(e, "quality", None) if not isinstance(e, dict) else e.get("quality")
        if score is not None:
            quality.append(float(score))
    if quality:
        vector.values["quality"] = ObjectiveValue(
            value=sum(quality) / len(quality),
            available=True,
            source=ObjectiveSource.REALIZED,
            evaluation_visibility=EvaluationVisibility.PUBLIC,
            evidence_count=len(quality),
        )
    return vector, failures
