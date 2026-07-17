"""Fast Loop budget accounting."""

from __future__ import annotations

from orchestra.control.fast_loop.budget import can_launch_candidate, spent_from_state
from orchestra.control.fast_loop.schemas import (
    CandidateRecord,
    CandidateStatus,
    CostRecord,
    FailureDiagnosis,
    FastLoopBudget,
    FastLoopState,
)
from orchestra.control.task_state import SubtaskFailureReason


def _state(*costs: CostRecord) -> FastLoopState:
    return FastLoopState(
        subtask_id="s1",
        base_attempt_id=1,
        base_graph_hash="h",
        diagnosis=FailureDiagnosis(
            reason=SubtaskFailureReason.HARNESS,
            retryable=True,
            concise_feedback="fail",
        ),
        candidates=[
            CandidateRecord(
                candidate_id=f"c{i}",
                attempt_id=1,
                graph_hash="g",
                parent_graph_hash="h",
                edits=[],
                status=CandidateStatus.VALID,
                cost=cost,
            )
            for i, cost in enumerate(costs)
        ],
    )


def test_spent_aggregates_candidate_costs():
    state = _state(
        CostRecord(backend_calls=1, estimated_cost_usd=0.2, prompt_tokens=10),
        CostRecord(backend_calls=2, estimated_cost_usd=0.3, prompt_tokens=5),
    )
    spent = spent_from_state(state)
    assert spent.backend_calls == 3
    assert spent.estimated_cost_usd == 0.5
    assert spent.prompt_tokens == 15


def test_launch_blocked_when_backend_calls_exhausted():
    state = _state(CostRecord(backend_calls=8))
    ok, reason = can_launch_candidate(
        FastLoopBudget(max_total_backend_calls=8),
        state,
        reserved_backend_calls=1,
    )
    assert ok is False
    assert reason is not None
