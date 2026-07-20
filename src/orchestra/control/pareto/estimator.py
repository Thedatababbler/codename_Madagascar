"""Evidence-based candidate objective estimation."""

from __future__ import annotations

from orchestra.control.pareto.archive import ParetoArchive
from orchestra.control.pareto.schemas import (
    ObjectiveSource,
    ObjectiveValue,
    ParetoEvaluationKind,
    ParetoOrchestraCandidate,
)
from orchestra.control.slow_loop.schemas import GlobalObservation, TaskBudgetRemaining


class ParetoObjectiveEstimator:
    def estimate(
        self,
        candidate: ParetoOrchestraCandidate,
        *,
        observation: GlobalObservation,
        task_budget: TaskBudgetRemaining | None = None,
        archive: ParetoArchive | None = None,
    ) -> ParetoOrchestraCandidate:
        values = candidate.objectives.values
        budget = task_budget or observation.remaining_task_budget
        if budget.budget_configured and budget.remaining_cost_usd is not None:
            values["cost"] = ObjectiveValue(
                value=max(0.0, budget.max_cost_usd - budget.remaining_cost_usd)
                if budget.max_cost_usd is not None
                else None,
                available=budget.max_cost_usd is not None,
                source=ObjectiveSource.DECLARED_BUDGET,
            )
        if observation.backend_latency_summary:
            values["latency"] = ObjectiveValue(
                value=sum(observation.backend_latency_summary.values())
                / len(observation.backend_latency_summary),
                available=True,
                source=ObjectiveSource.HISTORY,
                evidence_count=len(observation.backend_latency_summary),
            )
        values["risk"] = ObjectiveValue(
            value=float(sum(observation.recent_failure_counts.values())),
            available=True,
            source=ObjectiveSource.HISTORY,
            evidence_count=sum(observation.recent_failure_counts.values()),
        )
        values["communication_overhead"] = ObjectiveValue(
            value=candidate.communication_overhead, available=True, source=ObjectiveSource.ESTIMATED
        )
        # No harness evidence means explicitly unavailable quality.
        if "quality" not in values:
            values["quality"] = ObjectiveValue.unavailable(
                "quality estimator requires public harness evidence"
            )
        candidate.objectives.evaluation_kind = ParetoEvaluationKind.ESTIMATED
        return candidate
