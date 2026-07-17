"""Candidate costs must not be double-counted into search_cost."""

from __future__ import annotations

from orchestra.control.fast_loop.budget import spent_from_state
from orchestra.control.fast_loop.schemas import (
    CandidateRecord,
    CandidateStatus,
    CostRecord,
    FailureDiagnosis,
    FastLoopState,
)
from orchestra.control.task_state import SubtaskFailureReason


def _fl(*costs: CostRecord) -> FastLoopState:
    return FastLoopState(
        subtask_id="s1",
        base_attempt_id=1,
        base_graph_hash="h",
        diagnosis=FailureDiagnosis(
            reason=SubtaskFailureReason.HARNESS,
            retryable=True,
            concise_feedback="fail",
            primary_failed_node_id="coder",
            failed_node_ids=["coder"],
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
        initial_execution_cost=CostRecord(backend_calls=1, estimated_cost_usd=0.05),
    )


def test_fast_loop_cost_not_double_counted():
    state = _fl(CostRecord(backend_calls=2, estimated_cost_usd=0.2, prompt_tokens=10))
    # search_cost is derived from candidates only once.
    assert state.search_cost.backend_calls == 2
    assert state.search_cost.estimated_cost_usd == 0.2
    spent = spent_from_state(state)
    assert spent.backend_calls == 2
    assert spent.estimated_cost_usd == 0.2


def test_loser_candidate_cost_is_preserved():
    state = _fl(
        CostRecord(backend_calls=1, estimated_cost_usd=0.1),
        CostRecord(backend_calls=3, estimated_cost_usd=0.3),
    )
    state.candidates[0].status = CandidateStatus.DISCARDED
    state.candidates[1].status = CandidateStatus.COMMITTED
    state.selected_execution_cost = state.candidates[1].cost
    assert state.search_cost.backend_calls == 4
    assert state.candidates[0].cost.backend_calls == 1


def test_total_method_cost_accounting():
    state = _fl(CostRecord(backend_calls=2, estimated_cost_usd=0.2))
    state.selected_execution_cost = state.candidates[0].cost
    total = state.total_method_cost
    assert total.backend_calls == 3  # initial + search
    assert abs(total.estimated_cost_usd - 0.25) < 1e-9
