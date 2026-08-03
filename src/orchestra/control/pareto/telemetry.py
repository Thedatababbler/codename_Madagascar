"""Decision-horizon telemetry extraction from public state ledgers."""

from __future__ import annotations

from typing import Any

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


def attributed_usage_for_decision(
    state: TaskExecutionState,
    *,
    decision_id: str | None,
    activated_revision_id: str | None,
    affected_subtask_ids: list[str],
    affected_wave_id: str | None,
    usage_start: int = 0,
) -> tuple[list[BackendUsageRecord], dict[str, Any]]:
    """Select usage belonging to the decision's attributed execution evidence.

    Preference order:
    1. usage_ids recorded on terminal attempts of affected subtasks under the
       activation revision;
    2. usage stamped with decision_id / matching plan_revision for affected
       subtasks;
    3. usage on the bound wave and later waves that share the activation
       revision for affected subtasks.

    Historical / unrelated pre-decision usage is never included.
    """
    all_records = horizon_usage_records(state, usage_start)
    by_id = {r.usage_id: r for r in all_records if r.usage_id}
    selected_ids: list[str] = []
    attempt_evidence: dict[str, Any] = {}
    for sid in affected_subtask_ids:
        sub = state.subtasks.get(sid)
        if sub is None:
            continue
        matching = [
            a
            for a in (sub.attempts or [])
            if getattr(a, "execution_plan_revision", None) == activated_revision_id
            and getattr(a, "wave_id", None)
        ]
        if not matching:
            continue
        last = matching[-1]
        ids = list(getattr(last, "usage_ids", None) or [])
        attempt_evidence[sid] = {
            "attempt_id": last.attempt_id,
            "wave_id": last.wave_id,
            "execution_plan_revision": last.execution_plan_revision,
            "scheduler_incarnation": last.scheduler_incarnation,
            "usage_ids": ids,
        }
        selected_ids.extend(ids)

    if selected_ids:
        records = [by_id[i] for i in selected_ids if i in by_id]
        return records, {
            "attribution": "attempt_usage_ids",
            "usage_ids": sorted({r.usage_id for r in records}),
            "attempt_evidence": attempt_evidence,
        }

    filtered: list[BackendUsageRecord] = []
    for rec in all_records:
        if rec.subtask_id not in set(affected_subtask_ids):
            continue
        if decision_id and rec.decision_id and rec.decision_id == decision_id:
            filtered.append(rec)
            continue
        if (
            activated_revision_id
            and rec.plan_revision == activated_revision_id
            and str(rec.phase or "") in {"post_activation", "recovery", ""}
        ):
            filtered.append(rec)
            continue
        if affected_wave_id and rec.wave_id == affected_wave_id:
            filtered.append(rec)
    if filtered:
        return filtered, {
            "attribution": "decision_wave_revision_filter",
            "usage_ids": sorted({r.usage_id for r in filtered if r.usage_id}),
            "attempt_evidence": attempt_evidence,
        }
    return [], {
        "attribution": "none",
        "usage_ids": [],
        "attempt_evidence": attempt_evidence,
        "reason": "missing_decision_wave_attribution",
    }


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
    decision_id: str | None = None,
    activated_revision_id: str | None = None,
    affected_subtask_ids: list[str] | None = None,
    affected_wave_id: str | None = None,
):
    if affected_subtask_ids is not None:
        records, attribution = attributed_usage_for_decision(
            state,
            decision_id=decision_id,
            activated_revision_id=activated_revision_id,
            affected_subtask_ids=list(affected_subtask_ids),
            affected_wave_id=affected_wave_id,
            usage_start=usage_start,
        )
    else:
        records = horizon_usage_records(state, usage_start)
        attribution = {
            "attribution": "baseline_index_slice",
            "usage_ids": sorted({r.usage_id for r in records if r.usage_id}),
        }
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
    cost = vector.values.get("cost")
    if cost is not None:
        detail = cost.detail
        if attribution.get("attribution") == "none":
            vector.values["cost"] = ObjectiveValue(
                value=None,
                available=False,
                source=ObjectiveSource.REALIZED,
                evaluation_visibility=EvaluationVisibility.PUBLIC,
                evidence_count=0,
                detail="missing_decision_wave_attribution",
            )
        elif cost.available:
            vector.values["cost"] = cost.model_copy(
                update={
                    "detail": (
                        f"decision_attributed:{attribution.get('attribution')}"
                    )
                }
            )
        elif detail is None:
            vector.values["cost"] = cost.model_copy(
                update={"detail": "partial cost unavailable"}
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
    return vector, failures, attribution
