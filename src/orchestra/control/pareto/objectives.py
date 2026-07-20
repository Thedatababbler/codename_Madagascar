"""Measured Pareto objectives using only public execution telemetry."""

from __future__ import annotations

from orchestra.control.backend_usage import BackendUsageRecord
from orchestra.control.pareto.schemas import (
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
    # Quality is intentionally unavailable unless a public harness/evidence producer
    # supplied it; no neutral score is invented.
    values = {
        "quality": ObjectiveValue.unavailable("no public harness quality evidence"),
        "cost": ObjectiveValue(
            value=sum(costs),
            available=bool(costs),
            source=ObjectiveSource.REALIZED,
            evaluation_visibility=ParetoEvaluationKind.REALIZED,
            evidence_count=len(costs),
        ),
        "latency": ObjectiveValue(
            value=sum(latencies),
            available=bool(latencies),
            source=ObjectiveSource.REALIZED,
            evaluation_visibility=ParetoEvaluationKind.REALIZED,
            evidence_count=len(latencies),
        ),
        "risk": ObjectiveValue(
            value=failure_risk(raw_failures, risk_coefficients),
            available=True,
            source=ObjectiveSource.REALIZED,
            evaluation_visibility=ParetoEvaluationKind.REALIZED,
            evidence_count=raw_failures.total,
        ),
        "communication_overhead": ObjectiveValue(
            value=float(delivery_count),
            available=True,
            source=ObjectiveSource.REALIZED,
            evaluation_visibility=ParetoEvaluationKind.REALIZED,
        ),
    }
    return ParetoObjectiveVector(values=values, evaluation_kind=ParetoEvaluationKind.REALIZED)
