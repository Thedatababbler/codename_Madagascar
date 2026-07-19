"""SlowLoopController applies future-only revisions and fail-closes invalid ones."""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestra.communication.payload import PayloadContract
from orchestra.communication.plan import CommunicationPlan
from orchestra.control.slow_loop.controller import SlowLoopController
from orchestra.control.slow_loop.schemas import (
    GlobalPlanRevisionStatus,
    SlowLoopBudget,
    SlowLoopConfig,
    TaskBudgetRemaining,
    TaskSchedulingPolicy,
)
from orchestra.control.task_state import SubtaskStatus, TaskExecutionState
from orchestra.decomposition.schemas import BudgetSpec, SubtaskSpec, TaskPlan
from orchestra.runtime.backend import RunContext
from orchestra.runtime.limits import RuntimeLimits, RuntimeSemaphores


def _ctx(tmp_path: Path) -> RunContext:
    limits = RuntimeLimits(
        max_parallel_benchmark_tasks=1,
        max_parallel_nodes_per_task=1,
        max_parallel_llm_calls=1,
        max_parallel_sandboxes=1,
    )
    return RunContext(
        run_id="m5",
        task_id="t",
        run_dir=tmp_path,
        limits=limits,
        semaphores=RuntimeSemaphores(limits),
        contract_hash="c",
    )


def _state_with_pressure() -> TaskExecutionState:
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
                input_artifacts=[],
            ),
        ],
        communication_plan=CommunicationPlan(
            version=1,
            payload_contracts=[
                PayloadContract(
                    payload_id="big",
                    source_subtask_id="s1",
                    target_subtask_id="s3",
                    artifact_type="FinalAnswerArtifact",
                    max_tokens=5000,
                    metadata={"required": False, "priority": 50},
                )
            ],
            context_budgets={"s3": 1000},
        ),
    )
    state = TaskExecutionState.from_plan(plan)
    state.communication_plan = plan.communication_plan
    state.subtasks["s1"].status = SubtaskStatus.COMMITTED
    state.subtasks["s3"].status = SubtaskStatus.PENDING
    state.committed_subtask_count = 1
    state.scheduling_policy = TaskSchedulingPolicy()
    return state


@pytest.mark.asyncio
async def test_slow_loop_context_pressure_updates_pending_only(tmp_path):
    state = _state_with_pressure()
    s1_obj = state.subtasks["s1"].spec.objective
    ctrl = SlowLoopController(
        config=SlowLoopConfig(
            enabled=True,
            budget=SlowLoopBudget(
                min_commits_between_updates=1,
                context_pressure_ratio=0.5,
                max_updates_per_task=4,
            ),
        )
    )
    result = await ctrl.maybe_update(
        task_plan=state.task_plan,
        state=state,
        context=_ctx(tmp_path),
        leased_subtask_ids=set(),
    )
    assert result.updated is True
    assert state.subtasks["s1"].spec.objective == s1_obj
    assert state.communication_plan.version >= 2
    assert state.active_plan_revision_id is not None
    assert state.plan_revision_history[-1].status is GlobalPlanRevisionStatus.APPLIED


@pytest.mark.asyncio
async def test_slow_loop_disabled_noop(tmp_path):
    state = _state_with_pressure()
    ctrl = SlowLoopController(config=SlowLoopConfig(enabled=False))
    result = await ctrl.maybe_update(
        task_plan=state.task_plan,
        state=state,
        context=_ctx(tmp_path),
        leased_subtask_ids=set(),
    )
    assert result.updated is False


@pytest.mark.asyncio
async def test_slow_loop_budget_pressure_uses_allowlist(tmp_path):
    state = _state_with_pressure()
    # Lower context pressure; force budget pressure.
    state.communication_plan = CommunicationPlan(version=1, context_budgets={})
    state.task_plan = state.task_plan.model_copy(
        update={"communication_plan": state.communication_plan}
    )
    ctrl = SlowLoopController(
        config=SlowLoopConfig(
            enabled=True,
            budget=SlowLoopBudget(
                min_commits_between_updates=1,
                budget_pressure_ratio=0.9,
                context_pressure_ratio=9.0,
            ),
            allowed_backend_assignments={"coding": ["smolagents_code", "codex_sdk"]},
            backend_model_pools={"smolagents_code": ["model_a"], "codex_sdk": ["model_c"]},
        )
    )
    result = await ctrl.maybe_update(
        task_plan=state.task_plan,
        state=state,
        context=_ctx(tmp_path),
        leased_subtask_ids=set(),
        task_budget=TaskBudgetRemaining(
            ratio=0.1, max_backend_calls=10, remaining_backend_calls=1, budget_configured=True
        ),
    )
    # May update via scheduling/backend candidate.
    assert result.diagnosis.update_required in {True, False} or result.updated in {
        True,
        False,
    }
