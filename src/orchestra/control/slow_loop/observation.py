"""Build GlobalObservation from public task state and telemetry."""

from __future__ import annotations

from orchestra.communication.ledger import DeliveryFailureReason, DeliveryStatus
from orchestra.communication.projection import estimate_tokens
from orchestra.control.fast_loop.budget import add_costs
from orchestra.control.fast_loop.schemas import CostRecord, sum_candidate_costs
from orchestra.control.slow_loop.evidence import (
    RuntimeEvidenceKind,
    collect_active_block_events,
    collect_runtime_evidence_events,
)
from orchestra.control.slow_loop.schemas import (
    DeliveryStatistics,
    GlobalObservation,
    SlowLoopState,
    TaskBudgetRemaining,
    TaskSchedulingPolicy,
)
from orchestra.control.task_state import (
    SubtaskFailureReason,
    SubtaskStatus,
    TaskExecutionState,
    WorkspaceCommitStatus,
)

_SKIPPED_STATUSES = {
    DeliveryStatus.SKIPPED,
    DeliveryStatus.SKIPPED_CONDITION_FALSE,
    DeliveryStatus.SKIPPED_RULE_DISABLED,
    DeliveryStatus.SKIPPED_NO_RULE,
    DeliveryStatus.SKIPPED_TARGET_NOT_DELIVERABLE,
}

_AGGREGATION_REASONS = {
    DeliveryFailureReason.AGGREGATION_CONFLICT.value,
    DeliveryFailureReason.AGGREGATION_RULE_MISSING.value,
    DeliveryFailureReason.AMBIGUOUS_AGGREGATION_RULE.value,
    DeliveryFailureReason.AGGREGATION_INPUT_SET_MISMATCH.value,
    DeliveryFailureReason.AGGREGATION_REQUIRED_INPUT_MISSING.value,
}

_REQUIRED_BLOCK_REASONS = {
    DeliveryFailureReason.REQUIRED_SOURCE_NOT_COMMITTED.value,
    DeliveryFailureReason.REQUIRED_ARTIFACT_MISSING.value,
    DeliveryFailureReason.REQUIRED_FIELD_MISSING.value,
    DeliveryFailureReason.REQUIRED_RULE_MISSING.value,
    DeliveryFailureReason.REQUIRED_CONDITION_UNSATISFIED.value,
    DeliveryFailureReason.CONTEXT_BUDGET_INFEASIBLE.value,
    DeliveryFailureReason.PROJECTION_INFEASIBLE.value,
    DeliveryFailureReason.LEDGER_CORRUPTION.value,
}

_NON_FAILURE_REASONS = {
    DeliveryFailureReason.TARGET_NOT_DELIVERABLE.value,
}


def _reason_str(reason: object | None) -> str:
    if reason is None:
        return ""
    if isinstance(reason, DeliveryFailureReason):
        return reason.value
    return str(reason)


def _compute_delivery_stats(
    records: list,
    *,
    active_block_reasons: dict[str, str] | None = None,
) -> DeliveryStatistics:
    delivered = [d for d in records if d.status is DeliveryStatus.DELIVERED]
    delivery_failure_counts: dict[str, int] = {}
    aggregation_failure_counts: dict[str, int] = {}
    required_blocks = 0
    for d in records:
        reason_s = _reason_str(getattr(d, "failure_reason", None))
        if reason_s in _NON_FAILURE_REASONS:
            continue
        if d.status is DeliveryStatus.FAILED or reason_s:
            if reason_s:
                delivery_failure_counts[reason_s] = (
                    delivery_failure_counts.get(reason_s, 0) + 1
                )
            if reason_s in _AGGREGATION_REASONS:
                aggregation_failure_counts[reason_s] = (
                    aggregation_failure_counts.get(reason_s, 0) + 1
                )
            if reason_s in _REQUIRED_BLOCK_REASONS:
                required_blocks += 1
    for br in (active_block_reasons or {}).values():
        if not br or br in _NON_FAILURE_REASONS:
            continue
        delivery_failure_counts[br] = delivery_failure_counts.get(br, 0) + 1
        if br in _AGGREGATION_REASONS:
            aggregation_failure_counts[br] = aggregation_failure_counts.get(br, 0) + 1
        if br in _REQUIRED_BLOCK_REASONS:
            required_blocks += 1
    return DeliveryStatistics(
        delivered_count=len(delivered),
        skipped_count=sum(1 for d in records if d.status in _SKIPPED_STATUSES),
        failed_count=sum(1 for d in records if d.status is DeliveryStatus.FAILED),
        unique_payloads=len({d.payload_id for d in delivered}),
        delivery_failure_counts=delivery_failure_counts,
        aggregation_failure_counts=aggregation_failure_counts,
        required_delivery_block_count=required_blocks,
    )


def _slow_state(state: TaskExecutionState) -> SlowLoopState | None:
    slow = state.slow_loop_state
    if slow is None:
        return None
    if not isinstance(slow, SlowLoopState):
        return SlowLoopState.model_validate(slow)
    return slow


def collect_evidence_keys(
    state: TaskExecutionState,
    *,
    delivery_start: int = 0,
    commit_start: int = 0,
) -> list[str]:
    """Stable evidence keys from typed runtime events."""
    events = collect_runtime_evidence_events(
        state, delivery_start=delivery_start, commit_start=commit_start
    )
    return sorted({ev.evidence_key for ev in events})


def build_global_observation(
    state: TaskExecutionState,
    *,
    scheduling_policy: TaskSchedulingPolicy | None = None,
    task_budget: TaskBudgetRemaining | None = None,
) -> GlobalObservation:
    policy = scheduling_policy or state.scheduling_policy or TaskSchedulingPolicy()
    if not isinstance(policy, TaskSchedulingPolicy):
        policy = TaskSchedulingPolicy.model_validate(policy)

    committed: list[str] = []
    running: list[str] = []
    pending: list[str] = []
    ready: list[str] = []
    failed: list[str] = []
    leased: list[str] = []
    failure_counts: dict[str, int] = {}
    backend_failures: dict[str, int] = {}
    harness_failures = 0
    merge_conflicts = 0

    for sid, sub in sorted(state.subtasks.items()):
        if sub.lease_status == "leased":
            leased.append(sid)
        if sub.status is SubtaskStatus.COMMITTED:
            committed.append(sid)
        elif sub.status is SubtaskStatus.READY:
            ready.append(sid)
        elif sub.status is SubtaskStatus.PENDING:
            pending.append(sid)
        elif sub.status in {
            SubtaskStatus.RUNNING,
            SubtaskStatus.AWAITING_CANONICAL_COMMIT,
            SubtaskStatus.RETRY_PENDING,
        }:
            running.append(sid)
        elif sub.status in {SubtaskStatus.FAILED, SubtaskStatus.HARNESS_FAILED}:
            failed.append(sid)
        if sub.failure_reason is not None:
            key = str(sub.failure_reason.value)
            failure_counts[key] = failure_counts.get(key, 0) + 1
            if sub.failure_reason is SubtaskFailureReason.HARNESS:
                harness_failures += 1
            if sub.failure_reason is SubtaskFailureReason.CANONICAL_MERGE_CONFLICT:
                merge_conflicts += 1
        for sess in sub.backend_sessions:
            if sub.failure_reason in {
                SubtaskFailureReason.INFRA,
                SubtaskFailureReason.MODEL,
            }:
                backend_failures[sess.backend_id] = (
                    backend_failures.get(sess.backend_id, 0) + 1
                )

    for rec in state.workspace_commit_records:
        if rec.status is WorkspaceCommitStatus.CONFLICTED:
            merge_conflicts += 1
        if rec.status is WorkspaceCommitStatus.VALIDATION_FAILED:
            harness_failures += 1

    total_fast = CostRecord()
    for fl in state.fast_loop_states.values():
        if hasattr(fl, "search_cost"):
            total_fast = add_costs(total_fast, fl.search_cost)  # type: ignore[arg-type]
        elif isinstance(fl, dict) and "candidates" in fl:
            from orchestra.control.fast_loop.schemas import CandidateRecord

            cands = [CandidateRecord.model_validate(c) for c in fl.get("candidates", [])]
            total_fast = add_costs(total_fast, sum_candidate_costs(cands))

    total_exec = CostRecord(backend_calls=len(committed) + len(failed))
    total_exec = add_costs(total_exec, total_fast)

    token_estimates: dict[str, int] = {}
    for sid in committed:
        sub = state.subtasks[sid]
        for ref in sub.committed_artifacts:
            token_estimates[ref.artifact_id] = token_estimates.get(ref.artifact_id, 256)

    # Context pressure: only eligible future (PENDING/READY + UNLEASED) targets.
    pressure: dict[str, float] = {}
    for target, budget in state.communication_plan.context_budgets.items():
        if budget <= 0:
            continue
        sub = state.subtasks.get(target)
        if sub is None:
            continue
        if sub.lease_status == "leased":
            continue
        if sub.status not in {SubtaskStatus.PENDING, SubtaskStatus.READY}:
            continue
        est = 0
        for contract in state.communication_plan.payload_contracts:
            if contract.target_subtask_id != target:
                continue
            est += int(contract.max_tokens)
        pressure[target] = est / float(budget)

    slow = _slow_state(state)
    watermark_delivery = slow.last_observed_delivery_index if slow else 0
    watermark_commits = slow.last_observed_commit_record_index if slow else 0
    handled = set(slow.handled_evidence_keys) if slow else set()

    ledger = list(state.delivery_ledger or [])
    active_block_events = collect_active_block_events(state)
    lifetime_active_blocks = {
        ev.target_subtask_id: (ev.failure_reason or "")
        for ev in active_block_events
        if ev.target_subtask_id
    }
    recent_active_block_events = [
        ev for ev in active_block_events if ev.evidence_key not in handled
    ]
    recent_active_blocks = {
        ev.target_subtask_id: (ev.failure_reason or "")
        for ev in recent_active_block_events
        if ev.target_subtask_id
    }

    lifetime_stats = _compute_delivery_stats(
        ledger, active_block_reasons=lifetime_active_blocks
    )
    # Recent ledger slice: watermark + active communication plan version.
    active_version = state.communication_plan.version
    recent_ledger = [
        d
        for i, d in enumerate(ledger)
        if i >= watermark_delivery and d.communication_plan_version == active_version
    ]
    recent_stats = _compute_delivery_stats(
        recent_ledger, active_block_reasons=recent_active_blocks
    )

    all_events = collect_runtime_evidence_events(
        state, delivery_start=watermark_delivery, commit_start=watermark_commits
    )
    new_events = [ev for ev in all_events if ev.evidence_key not in handled]
    # Active block keys must appear in new_evidence_keys for watermark consumption.
    new_keys = sorted({ev.evidence_key for ev in new_events})

    recent_harness = sum(
        1 for ev in new_events if ev.kind is RuntimeEvidenceKind.HARNESS_FAILURE
    )
    recent_conflicts = sum(
        1 for ev in new_events if ev.kind is RuntimeEvidenceKind.CANONICAL_CONFLICT
    )
    recent_backend: dict[str, int] = {}
    for ev in new_events:
        if ev.kind is not RuntimeEvidenceKind.BACKEND_FAILURE:
            continue
        backend_id = ev.backend_id or "unknown"
        recent_backend[backend_id] = recent_backend.get(backend_id, 0) + 1

    # Lifetime backend/harness from full typed events (not watermark-sliced).
    lifetime_events = collect_runtime_evidence_events(state)
    lifetime_harness = sum(
        1 for ev in lifetime_events if ev.kind is RuntimeEvidenceKind.HARNESS_FAILURE
    )
    lifetime_backend: dict[str, int] = {}
    for ev in lifetime_events:
        if ev.kind is not RuntimeEvidenceKind.BACKEND_FAILURE:
            continue
        bid = ev.backend_id or "unknown"
        lifetime_backend[bid] = lifetime_backend.get(bid, 0) + 1
    lifetime_conflicts = sum(
        1
        for ev in lifetime_events
        if ev.kind is RuntimeEvidenceKind.CANONICAL_CONFLICT
    )

    commits = state.committed_subtask_count or len(committed)
    commits_since = commits - (slow.commits_at_last_update if slow else 0)

    rem = task_budget or TaskBudgetRemaining(ratio=1.0)
    return GlobalObservation(
        task_id=state.task_id,
        state_version=state.state_version,
        active_plan_version=state.task_plan.plan_version,
        active_communication_version=state.communication_plan.version,
        committed_subtasks=committed,
        running_subtasks=running,
        pending_subtasks=pending,
        ready_subtasks=ready,
        failed_subtasks=failed,
        leased_subtasks=leased,
        total_execution_cost=total_exec,
        total_fast_loop_cost=total_fast,
        remaining_task_budget=rem,
        recent_failure_counts=failure_counts,
        backend_failure_counts=lifetime_backend or backend_failures,
        backend_latency_summary={},
        canonical_merge_conflicts=max(merge_conflicts, lifetime_conflicts),
        harness_failures=max(harness_failures, lifetime_harness),
        artifact_token_estimates=token_estimates,
        target_context_pressure=pressure,
        delivery_statistics=lifetime_stats,
        lifetime_delivery_statistics=lifetime_stats,
        recent_delivery_statistics=recent_stats,
        lifetime_harness_failures=max(harness_failures, lifetime_harness),
        recent_harness_failures=recent_harness,
        lifetime_canonical_conflicts=max(merge_conflicts, lifetime_conflicts),
        recent_canonical_conflicts=recent_conflicts,
        lifetime_backend_failure_counts=dict(lifetime_backend or backend_failures),
        recent_backend_failure_counts=recent_backend,
        current_max_concurrency=policy.max_concurrent_subtasks,
        current_serialization_groups=list(policy.serialization_groups),
        commits_since_last_slow_update=max(0, commits_since),
        new_evidence_keys=new_keys,
        recent_active_block_reasons=recent_active_blocks,
    )


def advance_observation_watermark(
    state: TaskExecutionState,
    *,
    observation: GlobalObservation | None = None,
    evidence_keys: list[str] | None = None,
) -> SlowLoopState:
    """Update SlowLoopState watermarks after a diagnosed wave."""
    slow = _slow_state(state) or SlowLoopState()
    keys = list(slow.handled_evidence_keys)
    for k in evidence_keys or (observation.new_evidence_keys if observation else []):
        if k not in keys:
            keys.append(k)
    slow.handled_evidence_keys = sorted(set(keys))
    slow.last_observed_state_version = state.state_version
    slow.last_observed_delivery_index = len(state.delivery_ledger or [])
    slow.last_observed_commit_record_index = len(state.workspace_commit_records or [])
    slow.last_observed_fast_loop_history_index = len(state.fast_loop_history or [])
    state.slow_loop_state = slow
    return slow


# Silence unused import if estimate_tokens unused in some builds
_ = estimate_tokens
