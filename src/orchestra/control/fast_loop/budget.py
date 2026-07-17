"""Fast Loop budget accounting helpers."""

from __future__ import annotations

from orchestra.control.fast_loop.schemas import (
    CandidateRecord,
    CostRecord,
    FastLoopBudget,
    FastLoopState,
)


class BudgetExhausted(RuntimeError):
    pass


def add_costs(a: CostRecord, b: CostRecord) -> CostRecord:
    return CostRecord(
        prompt_tokens=a.prompt_tokens + b.prompt_tokens,
        completion_tokens=a.completion_tokens + b.completion_tokens,
        estimated_cost_usd=a.estimated_cost_usd + b.estimated_cost_usd,
        backend_calls=a.backend_calls + b.backend_calls,
    )


def spent_from_state(state: FastLoopState) -> CostRecord:
    total = CostRecord()
    for cand in state.candidates:
        total = add_costs(total, cand.cost)
    return add_costs(total, state.search_cost)


def remaining_budget(
    budget: FastLoopBudget,
    state: FastLoopState,
) -> dict[str, float | int | None]:
    spent = spent_from_state(state)
    return {
        "backend_calls": budget.max_total_backend_calls - spent.backend_calls,
        "cost": (
            None
            if budget.max_total_cost is None
            else budget.max_total_cost - spent.estimated_cost_usd
        ),
        "tokens": (
            None
            if budget.max_total_tokens is None
            else budget.max_total_tokens
            - (spent.prompt_tokens + spent.completion_tokens)
        ),
        "candidates": budget.max_candidates
        - len([c for c in state.candidates if c.status.value != "rejected"]),
    }


def can_launch_candidate(
    budget: FastLoopBudget,
    state: FastLoopState,
    *,
    reserved_backend_calls: int = 1,
) -> tuple[bool, str | None]:
    if len(state.candidates) >= budget.max_candidates and all(
        c.candidate_id for c in state.candidates
    ):
        # Allow launching already-registered pending candidates.
        pass
    spent = spent_from_state(state)
    if spent.backend_calls + reserved_backend_calls > budget.max_total_backend_calls:
        return False, "max_total_backend_calls exhausted"
    if (
        budget.max_total_cost is not None
        and spent.estimated_cost_usd > budget.max_total_cost
    ):
        return False, "max_total_cost exhausted"
    if budget.max_total_tokens is not None:
        tokens = spent.prompt_tokens + spent.completion_tokens
        if tokens > budget.max_total_tokens:
            return False, "max_total_tokens exhausted"
    return True, None


def record_candidate_cost(record: CandidateRecord, cost: CostRecord) -> CandidateRecord:
    return record.model_copy(update={"cost": add_costs(record.cost, cost)})
