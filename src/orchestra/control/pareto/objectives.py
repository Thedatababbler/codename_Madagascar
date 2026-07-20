"""Measured Pareto objectives using only public execution telemetry."""

from __future__ import annotations

from orchestra.control.backend_usage import BackendUsageRecord
from orchestra.control.pareto.schemas import (
    EvaluationVisibility,
    ObjectiveSource,
    ObjectiveValue,
    ParetoEvaluationKind,
    ParetoObjectiveVector,
    RawFailureCounts,
)


def failure_risk(counts: RawFailureCounts, coefficients: dict[str, float]) -> float:
    return sum(
        float(coefficients.get(name, 1.0)) * value for name, value in counts.model_dump().items()
    )


def objective_vector_from_usage(
    records: list[BackendUsageRecord],
    *,
    raw_failures: RawFailureCounts,
    risk_coefficients: dict[str, float],
    delivery_count: int = 0,
) -> ParetoObjectiveVector:
    costs = [r.estimated_cost_usd for r in records if r.estimated_cost_usd is not None]
    latencies = [r.latency_seconds for r in records if r.latency_seconds is not None]
    # Cost is complete only when every horizon record has a known cost.
    cost_complete = bool(records) and len(costs) == len(records)
    # Quality is intentionally unavailable unless a public harness/evidence producer
    # supplied it; no neutral score is invented.
    values = {
        "quality": ObjectiveValue.unavailable("no public harness quality evidence"),
        "cost": ObjectiveValue(
            value=sum(costs) if cost_complete else None,
            available=cost_complete,
            source=ObjectiveSource.REALIZED,
            evaluation_visibility=EvaluationVisibility.PUBLIC,
            evidence_count=len(costs),
            detail=None if cost_complete else "partial cost unavailable",
        ),
        "latency": ObjectiveValue(
            # Placeholder until wall clock is stamped by realized_horizon_objectives.
            value=max(latencies) if latencies else None,
            available=bool(latencies),
            source=ObjectiveSource.REALIZED,
            evaluation_visibility=EvaluationVisibility.PUBLIC,
            evidence_count=len(latencies),
            detail="sum_node_latency_placeholder",
        ),
        "risk": ObjectiveValue(
            value=failure_risk(raw_failures, risk_coefficients),
            available=True,
            source=ObjectiveSource.REALIZED,
            evaluation_visibility=EvaluationVisibility.PUBLIC,
            evidence_count=raw_failures.total,
        ),
        "communication_overhead": ObjectiveValue(
            value=float(delivery_count),
            available=True,
            source=ObjectiveSource.REALIZED,
            evaluation_visibility=EvaluationVisibility.PUBLIC,
        ),
        "sum_node_latency": ObjectiveValue(
            value=sum(latencies) if latencies else None,
            available=bool(latencies),
            source=ObjectiveSource.REALIZED,
            evaluation_visibility=EvaluationVisibility.PUBLIC,
            evidence_count=len(latencies),
        ),
    }
    return ParetoObjectiveVector(values=values, evaluation_kind=ParetoEvaluationKind.REALIZED)
