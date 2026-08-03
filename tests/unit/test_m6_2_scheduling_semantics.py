"""M6.2 correctness-closure unit tests (typed config, no-op rejection, latency)."""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestra.control.pareto.estimator import ParetoObjectiveEstimator
from orchestra.control.pareto.schemas import ParetoOrchestraCandidate
from orchestra.control.scheduling_effect import (
    SchedulingNoOpReason,
    assess_concurrency_edit,
    effective_concurrency,
    future_ready_waves,
)
from orchestra.control.slow_loop.observation import build_global_observation
from orchestra.control.slow_loop.schemas import SchedulingConcurrencyEdit, TaskSchedulingPolicy
from orchestra.control.task_state import SubtaskStatus, TaskExecutionState
from orchestra.decomposition.schemas import BudgetSpec, SubtaskSpec, TaskPlan
from orchestra.experiments.stage2_fixture import stage2_fixture_plan
from orchestra.runtime.limits import RuntimeLimits

REPO = Path(__file__).resolve().parents[2]


def _serial_plan() -> TaskPlan:
    graph = "configs/graphs/codex_single_implementer.yaml"
    return TaskPlan(
        task_id="serial",
        plan_version=1,
        decomposition_rationale="serial",
        subtasks=[
            SubtaskSpec(
                subtask_id="a",
                title="a",
                objective="a",
                dependencies=[],
                keystone_harness_id="none",
                local_graph_template=graph,
                budget=BudgetSpec(),
            ),
            SubtaskSpec(
                subtask_id="b",
                title="b",
                objective="b",
                dependencies=["a"],
                keystone_harness_id="none",
                local_graph_template=graph,
                budget=BudgetSpec(),
            ),
            SubtaskSpec(
                subtask_id="c",
                title="c",
                objective="c",
                dependencies=["b"],
                keystone_harness_id="none",
                local_graph_template=graph,
                budget=BudgetSpec(),
            ),
        ],
    )


def test_effective_concurrency_min_cap_and_policy():
    assert effective_concurrency(runtime_concurrency_cap=4, policy_concurrency=2) == 2
    assert effective_concurrency(runtime_concurrency_cap=1, policy_concurrency=8) == 1


def test_runtime_limits_exposes_typed_concurrency_cap():
    limits = RuntimeLimits(max_concurrent_subtasks=3)
    assert limits.max_concurrent_subtasks == 3


def test_runtime_cap_dominated_rejected():
    plan = stage2_fixture_plan()
    state = TaskExecutionState.from_plan(plan)
    state.subtasks["s1"].status = SubtaskStatus.COMMITTED
    state.scheduling_policy = TaskSchedulingPolicy(max_concurrent_subtasks=2)
    result = assess_concurrency_edit(
        plan=plan,
        state=state,
        current_policy=state.scheduling_policy,
        proposed_concurrency=8,
        runtime_concurrency_cap=2,
    )
    assert result.ok is False
    assert result.reason is SchedulingNoOpReason.RUNTIME_CAP_DOMINATED


def test_serial_dag_concurrency_rejected_as_noop():
    plan = _serial_plan()
    state = TaskExecutionState.from_plan(plan)
    state.subtasks["a"].status = SubtaskStatus.COMMITTED
    state.scheduling_policy = TaskSchedulingPolicy(max_concurrent_subtasks=1)
    waves = future_ready_waves(plan=plan, state=state)
    assert waves == [["b"], ["c"]]
    result = assess_concurrency_edit(
        plan=plan,
        state=state,
        current_policy=state.scheduling_policy,
        proposed_concurrency=2,
        runtime_concurrency_cap=4,
    )
    assert result.ok is False
    assert result.reason is SchedulingNoOpReason.NO_FUTURE_PARALLEL_WAVE


def test_fork_join_future_wave_allows_concurrency_increase():
    plan = stage2_fixture_plan()
    state = TaskExecutionState.from_plan(plan)
    state.subtasks["s1"].status = SubtaskStatus.COMMITTED
    state.scheduling_policy = TaskSchedulingPolicy(max_concurrent_subtasks=1)
    waves = future_ready_waves(plan=plan, state=state)
    assert waves[0] == ["s2", "s3"]
    result = assess_concurrency_edit(
        plan=plan,
        state=state,
        current_policy=state.scheduling_policy,
        proposed_concurrency=2,
        runtime_concurrency_cap=2,
    )
    assert result.ok is True
    assert result.proposed_effective == 2


def test_latency_estimator_no_parallel_speedup_on_serial_dag():
    plan = _serial_plan()
    state = TaskExecutionState.from_plan(plan)
    state.subtasks["a"].status = SubtaskStatus.COMMITTED
    state.scheduling_policy = TaskSchedulingPolicy(max_concurrent_subtasks=1)
    state.task_plan = plan.model_copy(
        update={
            "metadata": {
                "fixture_subtask_durations_seconds": {"a": 1.0, "b": 1.0, "c": 1.0},
            }
        }
    )
    est = ParetoObjectiveEstimator(runtime_concurrency_cap=4)
    edit = SchedulingConcurrencyEdit(max_concurrent_subtasks=2)
    cand = ParetoOrchestraCandidate(
        candidate_id="c1",
        content_hash="h1",
        edit_signature="sched:2",
        edits=[edit],
        context_id="ctx",
        global_candidate={"candidate_id": "c1"},
    )
    obs = build_global_observation(
        state, scheduling_policy=state.scheduling_policy, task_budget=None
    )
    lat = est._estimate_latency(
        cand, [], obs, {"scheduling_concurrency"}, state=state
    )
    assert lat.available is True
    # Serial future waves remain two unit steps even at eff=2.
    assert lat.value == pytest.approx(2.0)


def test_missing_latency_evidence_unavailable():
    plan = stage2_fixture_plan()
    state = TaskExecutionState.from_plan(plan)
    state.task_plan = plan.model_copy(update={"metadata": {}})
    state.scheduling_policy = TaskSchedulingPolicy(max_concurrent_subtasks=1)
    est = ParetoObjectiveEstimator(runtime_concurrency_cap=2)
    cand = ParetoOrchestraCandidate(
        candidate_id="c1",
        content_hash="h1",
        edit_signature="noop",
        edits=[],
        context_id="ctx",
        global_candidate={"candidate_id": "c1"},
    )
    obs = build_global_observation(
        state, scheduling_policy=state.scheduling_policy, task_budget=None
    )
    obs = obs.model_copy(update={"backend_latency_summary": {}})
    lat = est._estimate_latency(cand, [], obs, set(), state=state)
    assert lat.available is False
