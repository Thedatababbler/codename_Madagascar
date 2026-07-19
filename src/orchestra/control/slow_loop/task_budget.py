"""Task-level budget accounting for Slow Loop observation."""

from __future__ import annotations

from orchestra.control.fast_loop.budget import add_costs
from orchestra.control.fast_loop.schemas import CostRecord, sum_candidate_costs
from orchestra.control.slow_loop.schemas import TaskBudgetRemaining
from orchestra.control.task_state import SubtaskStatus, TaskExecutionState
from orchestra.decomposition.schemas import TaskPlan


class TaskBudgetTracker:
    """Derive TaskBudgetRemaining from plan metadata / configured ceilings."""

    def __init__(
        self,
        *,
        max_backend_calls: int | None = None,
        max_cost_usd: float | None = None,
    ) -> None:
        self.max_backend_calls = max_backend_calls
        self.max_cost_usd = max_cost_usd

    def snapshot(
        self,
        *,
        task_plan: TaskPlan,
        task_state: TaskExecutionState,
    ) -> TaskBudgetRemaining:
        meta = dict(task_plan.metadata or {})
        tb = meta.get("task_budget") if isinstance(meta.get("task_budget"), dict) else {}
        max_calls = self.max_backend_calls
        max_cost = self.max_cost_usd
        if max_calls is None and tb.get("max_backend_calls") is not None:
            max_calls = int(tb["max_backend_calls"])
        if max_cost is None and tb.get("max_cost_usd") is not None:
            max_cost = float(tb["max_cost_usd"])

        # Fallback: sum subtask BudgetSpec.max_llm_calls when explicitly marked.
        if max_calls is None and meta.get("use_subtask_llm_budget"):
            max_calls = sum(s.budget.max_llm_calls for s in task_plan.subtasks)

        if max_calls is None and max_cost is None:
            return TaskBudgetRemaining(
                ratio=1.0,
                budget_configured=False,
            )

        used = CostRecord()
        for fl in task_state.fast_loop_states.values():
            if hasattr(fl, "search_cost"):
                used = add_costs(used, fl.search_cost)  # type: ignore[arg-type]
            elif isinstance(fl, dict) and "candidates" in fl:
                from orchestra.control.fast_loop.schemas import CandidateRecord

                cands = [
                    CandidateRecord.model_validate(c) for c in fl.get("candidates", [])
                ]
                used = add_costs(used, sum_candidate_costs(cands))

        # Approximate execution spend: one backend call per terminal attempt.
        exec_calls = sum(
            1
            for sub in task_state.subtasks.values()
            if sub.status
            in {
                SubtaskStatus.COMMITTED,
                SubtaskStatus.FAILED,
                SubtaskStatus.HARNESS_FAILED,
            }
        )
        used = add_costs(used, CostRecord(backend_calls=exec_calls))

        rem_calls: int | None = None
        rem_cost: float | None = None
        ratios: list[float] = []
        if max_calls is not None and max_calls > 0:
            rem_calls = max(0, max_calls - used.backend_calls)
            ratios.append(rem_calls / float(max_calls))
        if max_cost is not None and max_cost > 0:
            rem_cost = max(0.0, max_cost - used.estimated_cost_usd)
            ratios.append(rem_cost / float(max_cost))

        ratio = min(ratios) if ratios else 1.0
        return TaskBudgetRemaining(
            max_backend_calls=max_calls,
            remaining_backend_calls=rem_calls,
            max_cost_usd=max_cost,
            remaining_cost_usd=rem_cost,
            ratio=ratio,
            budget_configured=True,
        )
