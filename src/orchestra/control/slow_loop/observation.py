"""Build GlobalObservation from public task state and telemetry."""

from __future__ import annotations

from orchestra.communication.ledger import DeliveryStatus
from orchestra.communication.projection import estimate_tokens
from orchestra.control.fast_loop.budget import add_costs
from orchestra.control.fast_loop.schemas import CostRecord, sum_candidate_costs
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
            # Rough public estimate without loading private evaluators.
            token_estimates[ref.artifact_id] = token_estimates.get(ref.artifact_id, 256)

    # Context pressure: estimate delivered tokens vs target budget.
    pressure: dict[str, float] = {}
    for target, budget in state.communication_plan.context_budgets.items():
        if budget <= 0:
            continue
        est = 0
        for contract in state.communication_plan.payload_contracts:
            if contract.target_subtask_id != target:
                continue
            est += int(contract.max_tokens)
        pressure[target] = est / float(budget)

    delivered = [
        d for d in state.delivery_ledger if d.status is DeliveryStatus.DELIVERED
    ]
    stats = DeliveryStatistics(
        delivered_count=len(delivered),
        skipped_count=sum(
            1 for d in state.delivery_ledger if d.status is DeliveryStatus.SKIPPED
        ),
        failed_count=sum(
            1 for d in state.delivery_ledger if d.status is DeliveryStatus.FAILED
        ),
        unique_payloads=len({d.payload_id for d in delivered}),
    )

    slow: SlowLoopState | None = state.slow_loop_state
    if slow is not None and not isinstance(slow, SlowLoopState):
        slow = SlowLoopState.model_validate(slow)
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
        backend_failure_counts=backend_failures,
        backend_latency_summary={},
        canonical_merge_conflicts=merge_conflicts,
        harness_failures=harness_failures,
        artifact_token_estimates=token_estimates,
        target_context_pressure=pressure,
        delivery_statistics=stats,
        current_max_concurrency=policy.max_concurrent_subtasks,
        current_serialization_groups=list(policy.serialization_groups),
        commits_since_last_slow_update=max(0, commits_since),
    )


# Silence unused import if estimate_tokens unused in some builds
_ = estimate_tokens
