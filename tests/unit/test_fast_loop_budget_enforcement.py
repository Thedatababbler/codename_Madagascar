"""All FastLoopBudget fields must gate launches."""

from __future__ import annotations

from orchestra.control.fast_loop.budget import FastLoopBudgetTracker
from orchestra.control.fast_loop.schemas import (
    CandidateRecord,
    CandidateRejectionReason,
    CandidateStatus,
    CostRecord,
    FailureDiagnosis,
    FastLoopBudget,
    FastLoopState,
)
from orchestra.control.task_state import SubtaskFailureReason


def _state() -> FastLoopState:
    return FastLoopState(
        subtask_id="s",
        base_attempt_id=1,
        base_graph_hash="h",
        diagnosis=FailureDiagnosis(
            reason=SubtaskFailureReason.HARNESS,
            retryable=True,
            concise_feedback="x",
            primary_failed_node_id="n",
            failed_node_ids=["n"],
        ),
    )


def test_fast_loop_wall_time_budget():
    clock = {"t": 100.0}

    def now() -> float:
        return clock["t"]

    tracker = FastLoopBudgetTracker(
        FastLoopBudget(max_wall_time_seconds=10), clock=now
    )
    state = _state()
    tracker.mark_started(state)
    assert state.started_monotonic == 100.0
    clock["t"] = 111.0
    ok, reason, code = tracker.can_start_candidate(state)
    assert ok is False
    assert code is CandidateRejectionReason.BUDGET_EXCEEDED
    assert "wall_time" in (reason or "")


def test_fast_loop_attempt_budget():
    tracker = FastLoopBudgetTracker(FastLoopBudget(max_attempts_per_subtask=2))
    state = _state()
    state.candidates = [
        CandidateRecord(
            candidate_id="c0",
            attempt_id=2,
            graph_hash="g",
            parent_graph_hash="h",
            edits=[],
            status=CandidateStatus.VALID,
            cost=CostRecord(backend_calls=1),
        )
    ]
    # initial(1) + c0(1) = 2 attempts used → cannot start another
    ok, _, code = tracker.can_start_candidate(state)
    assert ok is False
    assert code is CandidateRejectionReason.BUDGET_EXCEEDED


def test_the_incumbent_does_not_spend_an_attempt_it_already_paid_for():
    """It is the initial attempt, so the base count already includes it.

    Counted twice, a quality search configured for three candidates generates two
    and rejects the third as "max_attempts_per_subtask exhausted" — which is what
    the first pyjwt quality search did (EXP-20260810-06).
    """
    tracker = FastLoopBudgetTracker(FastLoopBudget(max_attempts_per_subtask=2))
    state = _state()
    state.candidates = [
        CandidateRecord(
            candidate_id="incumbent_first_pass",
            attempt_id=1,
            graph_hash="h",
            parent_graph_hash="h",
            edits=[],
            status=CandidateStatus.COMMITTED,
            cost=CostRecord(backend_calls=2),
            metadata={"incumbent": True},
        )
    ]

    ok, reason, _code = tracker.can_start_candidate(state)

    assert ok is True, reason


def test_fast_loop_backend_call_budget():
    tracker = FastLoopBudgetTracker(FastLoopBudget(max_total_backend_calls=2))
    state = _state()
    state.candidates = [
        CandidateRecord(
            candidate_id="c0",
            attempt_id=2,
            graph_hash="g",
            parent_graph_hash="h",
            edits=[],
            status=CandidateStatus.VALID,
            cost=CostRecord(backend_calls=2),
        )
    ]
    ok, reason, code = tracker.can_start_candidate(state, reserved_backend_calls=1)
    assert ok is False
    assert "backend_calls" in (reason or "")


def test_fast_loop_cost_budget():
    tracker = FastLoopBudgetTracker(FastLoopBudget(max_total_cost=0.5))
    state = _state()
    state.candidates = [
        CandidateRecord(
            candidate_id="c0",
            attempt_id=2,
            graph_hash="g",
            parent_graph_hash="h",
            edits=[],
            status=CandidateStatus.VALID,
            cost=CostRecord(estimated_cost_usd=0.6, backend_calls=1),
        )
    ]
    ok, reason, _ = tracker.can_start_candidate(state)
    assert ok is False
    assert "cost" in (reason or "")


def test_parallel_candidates_reserve_budget_before_launch():
    tracker = FastLoopBudgetTracker(FastLoopBudget(max_total_backend_calls=3))
    state = _state()
    # Reserve 2 calls for a parallel pair.
    ok, _, _ = tracker.can_start_candidate(state, reserved_backend_calls=2)
    assert ok is True
    ok2, _, code = tracker.can_start_candidate(state, reserved_backend_calls=4)
    assert ok2 is False
    assert code is CandidateRejectionReason.BUDGET_EXCEEDED
