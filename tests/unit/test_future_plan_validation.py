"""Future-only validation rejects past/leased edits."""

from __future__ import annotations

from orchestra.communication.plan import CommunicationPlan
from orchestra.control.slow_loop.schemas import (
    ContextBudgetEdit,
    PendingPriorityEdit,
    SlowLoopConfig,
)
from orchestra.control.slow_loop.validation import FuturePlanValidator
from orchestra.control.task_state import SubtaskStatus, TaskExecutionState
from orchestra.decomposition.schemas import BudgetSpec, SubtaskSpec, TaskPlan


def _state() -> TaskExecutionState:
    plan = TaskPlan(
        task_id="t",
        plan_version=1,
        decomposition_rationale="x",
        subtasks=[
            SubtaskSpec(
                subtask_id="s1",
                title="s1",
                objective="s1",
                dependencies=[],
                keystone_harness_id="h",
                local_graph_template="g.yaml",
                budget=BudgetSpec(),
                expected_outputs=[],
                priority=0,
            ),
            SubtaskSpec(
                subtask_id="s2",
                title="s2",
                objective="s2",
                dependencies=["s1"],
                keystone_harness_id="h",
                local_graph_template="g.yaml",
                budget=BudgetSpec(),
                expected_outputs=[],
                priority=0,
            ),
            SubtaskSpec(
                subtask_id="s3",
                title="s3",
                objective="s3",
                dependencies=["s1"],
                keystone_harness_id="h",
                local_graph_template="g.yaml",
                budget=BudgetSpec(),
                expected_outputs=[],
                priority=0,
            ),
        ],
        communication_plan=CommunicationPlan(version=1),
    )
    state = TaskExecutionState.from_plan(plan)
    state.subtasks["s1"].status = SubtaskStatus.COMMITTED
    state.subtasks["s2"].status = SubtaskStatus.RUNNING
    state.subtasks["s2"].lease_status = "leased"
    state.subtasks["s3"].status = SubtaskStatus.PENDING
    return state


def test_committed_and_leased_edits_rejected_pending_allowed():
    state = _state()
    # Propose plan that changes s1, s2, s3 priorities.
    new_subs = []
    for spec in state.task_plan.subtasks:
        if spec.subtask_id == "s3":
            new_subs.append(spec.model_copy(update={"priority": 9}))
        elif spec.subtask_id == "s1":
            new_subs.append(spec.model_copy(update={"objective": "mutated"}))
        elif spec.subtask_id == "s2":
            new_subs.append(spec.model_copy(update={"priority": 5}))
        else:
            new_subs.append(spec)
    proposed = state.task_plan.model_copy(
        update={
            "subtasks": new_subs,
            "plan_version": 2,
            "communication_plan": CommunicationPlan(version=2),
        }
    )
    result = FuturePlanValidator(SlowLoopConfig(enabled=True)).validate(
        current_state=state,
        proposed_plan=proposed,
        edits=[
            PendingPriorityEdit(subtask_id="s1", priority=1),
            PendingPriorityEdit(subtask_id="s2", priority=1),
            PendingPriorityEdit(subtask_id="s3", priority=9),
        ],
        leased_subtask_ids={"s2"},
    )
    assert result.ok is False
    assert any("s1" in e for e in result.errors)
    assert any("s2" in e for e in result.errors)


def test_pending_only_edit_validates():
    state = _state()
    new_subs = []
    for spec in state.task_plan.subtasks:
        if spec.subtask_id == "s3":
            new_subs.append(spec.model_copy(update={"priority": 9}))
        else:
            new_subs.append(spec)
    proposed = state.task_plan.model_copy(
        update={
            "subtasks": new_subs,
            "plan_version": 2,
            "communication_plan": CommunicationPlan(
                version=2, context_budgets={"s3": 512}
            ),
        }
    )
    result = FuturePlanValidator(SlowLoopConfig(enabled=True)).validate(
        current_state=state,
        proposed_plan=proposed,
        edits=[
            PendingPriorityEdit(subtask_id="s3", priority=9),
            ContextBudgetEdit(target_subtask_id="s3", max_tokens=512),
        ],
        leased_subtask_ids={"s2"},
    )
    assert result.ok is True
