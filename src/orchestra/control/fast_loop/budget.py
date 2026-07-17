"""Fast Loop budget accounting helpers (single source of truth for costs)."""

from __future__ import annotations

import time
from collections.abc import Callable

from orchestra.control.fast_loop.schemas import (
    CandidateRecord,
    CandidateRejectionReason,
    CandidateStatus,
    CostRecord,
    FastLoopBudget,
    FastLoopState,
    sum_candidate_costs,
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
    """Candidate execution spend only (no double-count with search_cost)."""
    return add_costs(sum_candidate_costs(state.candidates), state.control_plane_cost)


def remaining_budget(
    budget: FastLoopBudget,
    state: FastLoopState,
) -> dict[str, float | int | None]:
    spent = spent_from_state(state)
    launched = [
        c
        for c in state.candidates
        if c.status
        not in {CandidateStatus.REJECTED, CandidateStatus.PENDING, CandidateStatus.DISCARDED}
        or c.cost.backend_calls > 0
    ]
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
        "candidates": budget.max_candidates - len(launched),
    }


class FastLoopBudgetTracker:
    """Enforce all FastLoopBudget fields at generate/launch/commit gates."""

    def __init__(
        self,
        budget: FastLoopBudget,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.budget = budget
        self._clock = clock or time.monotonic
        self._started: float | None = None

    def mark_started(self, state: FastLoopState) -> None:
        if state.started_monotonic is None:
            state.started_monotonic = self._clock()
        self._started = state.started_monotonic

    def _elapsed(self, state: FastLoopState) -> float:
        start = state.started_monotonic
        if start is None:
            return 0.0
        return max(0.0, self._clock() - start)

    def _attempts_used(self, state: FastLoopState) -> int:
        # base attempt + candidates that started (non-rejected-without-run)
        used = 1  # initial attempt that triggered fast loop
        for cand in state.candidates:
            if cand.status is CandidateStatus.REJECTED and cand.cost.backend_calls == 0:
                continue
            if cand.status is CandidateStatus.PENDING:
                continue
            used += 1
        return used

    def can_generate_candidate(
        self, state: FastLoopState
    ) -> tuple[bool, str | None, CandidateRejectionReason | None]:
        self.mark_started(state)
        if self._elapsed(state) >= self.budget.max_wall_time_seconds:
            return (
                False,
                "max_wall_time_seconds exhausted",
                CandidateRejectionReason.BUDGET_EXCEEDED,
            )
        pending_slots = self.budget.max_candidates - len(
            [c for c in state.candidates if c.status is not CandidateStatus.REJECTED]
        )
        if pending_slots <= 0 and state.candidates:
            return False, "max_candidates exhausted", CandidateRejectionReason.BUDGET_EXCEEDED
        if self._attempts_used(state) >= self.budget.max_attempts_per_subtask:
            return (
                False,
                "max_attempts_per_subtask exhausted",
                CandidateRejectionReason.BUDGET_EXCEEDED,
            )
        return True, None, None

    def can_start_candidate(
        self,
        state: FastLoopState,
        *,
        reserved_backend_calls: int = 1,
        reserved_cost: float = 0.0,
        reserved_tokens: int = 0,
    ) -> tuple[bool, str | None, CandidateRejectionReason | None]:
        self.mark_started(state)
        if self._elapsed(state) >= self.budget.max_wall_time_seconds:
            return (
                False,
                "max_wall_time_seconds exhausted",
                CandidateRejectionReason.BUDGET_EXCEEDED,
            )
        if self._attempts_used(state) >= self.budget.max_attempts_per_subtask:
            return (
                False,
                "max_attempts_per_subtask exhausted",
                CandidateRejectionReason.BUDGET_EXCEEDED,
            )
        spent = spent_from_state(state)
        if spent.backend_calls + reserved_backend_calls > self.budget.max_total_backend_calls:
            return (
                False,
                "max_total_backend_calls exhausted",
                CandidateRejectionReason.BUDGET_EXCEEDED,
            )
        if (
            self.budget.max_total_cost is not None
            and spent.estimated_cost_usd + reserved_cost > self.budget.max_total_cost
        ):
            return False, "max_total_cost exhausted", CandidateRejectionReason.BUDGET_EXCEEDED
        if self.budget.max_total_tokens is not None:
            tokens = spent.prompt_tokens + spent.completion_tokens + reserved_tokens
            if tokens > self.budget.max_total_tokens:
                return (
                    False,
                    "max_total_tokens exhausted",
                    CandidateRejectionReason.BUDGET_EXCEEDED,
                )
        return True, None, None

    def record_execution(self, record: CandidateRecord, cost: CostRecord) -> CandidateRecord:
        record.cost = add_costs(record.cost, cost)
        return record

    def remaining(self, state: FastLoopState) -> dict[str, float | int | None]:
        rem = remaining_budget(self.budget, state)
        rem["wall_time_seconds"] = max(
            0.0, self.budget.max_wall_time_seconds - self._elapsed(state)
        )
        rem["attempts"] = max(0, self.budget.max_attempts_per_subtask - self._attempts_used(state))
        return rem


def can_launch_candidate(
    budget: FastLoopBudget,
    state: FastLoopState,
    *,
    reserved_backend_calls: int = 1,
) -> tuple[bool, str | None]:
    ok, reason, _ = FastLoopBudgetTracker(budget).can_start_candidate(
        state, reserved_backend_calls=reserved_backend_calls
    )
    return ok, reason


def record_candidate_cost(record: CandidateRecord, cost: CostRecord) -> CandidateRecord:
    return record.model_copy(update={"cost": add_costs(record.cost, cost)})
