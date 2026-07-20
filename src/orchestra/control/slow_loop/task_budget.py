"""Task-level budget accounting for Slow Loop observation."""

from __future__ import annotations

from orchestra.control.backend_usage import (
    BackendUsageRecord,
    ObjectiveAccountingQuality,
    summarize_objective_accounting,
)
from orchestra.control.fast_loop.budget import add_costs
from orchestra.control.fast_loop.schemas import CostRecord, sum_candidate_costs
from orchestra.control.slow_loop.schemas import TaskBudgetRemaining
from orchestra.control.task_state import SubtaskStatus, TaskExecutionState
from orchestra.decomposition.schemas import TaskPlan


class TaskBudgetTracker:
    """Derive TaskBudgetRemaining from plan metadata / configured ceilings.

    Accounting sources (when present):
    - backend call accounting: ``TaskExecutionState.backend_usage_records``
    - fast-loop search cost: ``FastLoopState.search_cost`` / candidate costs
    - initial execution usage: included in usage records / candidate costs
    - slow-loop control usage: ``SlowLoopState.control_plane_cost``

    ``accounting_quality == \"exact\"`` only when every objective dimension
    used to compute the snapshot ratio is exact. Session existence alone is
    never sufficient for exact cost accounting.
    """

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

        if max_calls is None and meta.get("use_subtask_llm_budget"):
            max_calls = sum(s.budget.max_llm_calls for s in task_plan.subtasks)

        if max_calls is None and max_cost is None:
            return TaskBudgetRemaining(
                ratio=1.0,
                budget_configured=False,
                accounting_quality="approximate",
                objective_accounting=ObjectiveAccountingQuality(),
            )

        usage_records = [
            r
            if isinstance(r, BackendUsageRecord)
            else BackendUsageRecord.model_validate(r)
            for r in (task_state.backend_usage_records or [])
        ]
        used = CostRecord()
        obj = ObjectiveAccountingQuality()

        if usage_records:
            obj = summarize_objective_accounting(usage_records)
            used = add_costs(
                used,
                CostRecord(
                    backend_calls=len(usage_records),
                    prompt_tokens=sum(int(r.prompt_tokens or 0) for r in usage_records),
                    completion_tokens=sum(
                        int(r.completion_tokens or 0) for r in usage_records
                    ),
                    estimated_cost_usd=sum(
                        float(r.estimated_cost_usd or 0.0) for r in usage_records
                    ),
                ),
            )
        else:
            # Approximate path: sessions / terminals are not exact objectives.
            session_calls = sum(
                len(list(sub.backend_sessions or []))
                for sub in task_state.subtasks.values()
            )
            if session_calls > 0:
                used = add_costs(used, CostRecord(backend_calls=session_calls))
                obj = ObjectiveAccountingQuality(
                    backend_calls="approximate",
                    tokens="unavailable",
                    cost="unavailable",
                    latency="unavailable",
                )
            else:
                for fl in task_state.fast_loop_states.values():
                    if hasattr(fl, "search_cost"):
                        used = add_costs(used, fl.search_cost)  # type: ignore[arg-type]
                    elif isinstance(fl, dict) and "candidates" in fl:
                        from orchestra.control.fast_loop.schemas import CandidateRecord

                        cands = [
                            CandidateRecord.model_validate(c)
                            for c in fl.get("candidates", [])
                        ]
                        used = add_costs(used, sum_candidate_costs(cands))
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
                obj = ObjectiveAccountingQuality(
                    backend_calls="approximate",
                    tokens="approximate",
                    cost="unavailable",
                    latency="unavailable",
                )
            slow = task_state.slow_loop_state
            if slow is not None:
                ctrl = getattr(slow, "control_plane_cost", None)
                if ctrl is not None:
                    used = add_costs(used, ctrl)

        rem_calls: int | None = None
        rem_cost: float | None = None
        ratios: list[float] = []
        used_dims: list[str] = []
        if max_calls is not None and max_calls > 0:
            rem_calls = max(0, max_calls - used.backend_calls)
            ratios.append(rem_calls / float(max_calls))
            used_dims.append("backend_calls")
        if max_cost is not None and max_cost > 0:
            rem_cost = max(0.0, max_cost - used.estimated_cost_usd)
            ratios.append(rem_cost / float(max_cost))
            used_dims.append("cost")

        ratio = min(ratios) if ratios else 1.0
        quality = "exact"
        for dim in used_dims:
            dim_q = getattr(obj, dim)
            # derived cost is acceptable for exact-enough call accounting when
            # only backend_calls are constrained; cost dimension itself must be
            # exact or derived to keep summary exact when cost is used.
            if dim == "cost" and dim_q in {"exact", "derived"}:
                continue
            if dim_q != "exact":
                quality = "approximate"
                break
        if not used_dims:
            quality = "approximate"
        if "cost" in used_dims and obj.cost == "derived" and quality == "exact":
            # Summary remains exact for call+derived-cost snapshots.
            pass
        if "cost" in used_dims and obj.cost not in {"exact", "derived"}:
            quality = "approximate"

        return TaskBudgetRemaining(
            max_backend_calls=max_calls,
            remaining_backend_calls=rem_calls,
            max_cost_usd=max_cost,
            remaining_cost_usd=rem_cost,
            ratio=ratio,
            budget_configured=True,
            accounting_quality=quality,  # type: ignore[arg-type]
            objective_accounting=obj,
        )
