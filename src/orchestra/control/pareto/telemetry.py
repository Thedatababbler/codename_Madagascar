"""Decision-horizon telemetry extraction from public state ledgers."""

from __future__ import annotations

from orchestra.control.backend_usage import BackendUsageRecord
from orchestra.control.pareto.objectives import objective_vector_from_usage
from orchestra.control.pareto.schemas import ParetoConfig, RawFailureCounts
from orchestra.control.task_state import TaskExecutionState


def horizon_usage_records(state: TaskExecutionState, start_index: int) -> list[BackendUsageRecord]:
    return [
        r if isinstance(r, BackendUsageRecord) else BackendUsageRecord.model_validate(r)
        for r in (state.backend_usage_records or [])[start_index:]
    ]


def raw_failure_counts(
    state: TaskExecutionState, *, delivery_start: int = 0, commit_start: int = 0
) -> RawFailureCounts:
    delivery = list(state.delivery_ledger or [])[delivery_start:]
    commits = list(state.workspace_commit_records or [])[commit_start:]
    return RawFailureCounts(
        backend_failures=sum(
            1
            for r in state.backend_usage_records or []
            if str(getattr(r, "status", "")).lower() not in {"ok", "success", "completed"}
        ),
        delivery_failures=sum(
            1 for r in delivery if str(getattr(r, "status", "")).lower() in {"failed", "error"}
        ),
        canonical_conflicts=sum(
            1 for r in commits if str(getattr(r, "status", "")).lower() == "conflicted"
        ),
    )


def realized_horizon_objectives(
    state: TaskExecutionState,
    *,
    usage_start: int,
    delivery_start: int,
    commit_start: int,
    config: ParetoConfig,
):
    records = horizon_usage_records(state, usage_start)
    failures = raw_failure_counts(state, delivery_start=delivery_start, commit_start=commit_start)
    return objective_vector_from_usage(
        records,
        raw_failures=failures,
        risk_coefficients=config.risk_coefficients,
        delivery_count=len((state.delivery_ledger or [])[delivery_start:]),
    ), failures
