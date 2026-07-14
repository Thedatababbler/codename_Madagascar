"""Fallback and decomposer behavior."""

from __future__ import annotations

from orchestra.decomposition.decomposer import TaskDecomposer
from orchestra.decomposition.fallback import build_single_subtask_plan
from orchestra.decomposition.schemas import DecompositionStatus
from orchestra.decomposition.validator import validate_task_plan

GRAPH = "configs/graphs/b0_direct.yaml"


def test_build_single_subtask_plan_is_valid():
    plan = build_single_subtask_plan(
        task_id="echo",
        objective="Echo input",
        local_graph_template=GRAPH,
        keystone_harness_id="none",
    )
    assert plan.decomposition_status is DecompositionStatus.FALLBACK_SINGLE_SUBTASK
    assert len(plan.subtasks) == 1
    validate_task_plan(plan)


def test_decomposer_disabled_uses_single_subtask():
    decomposer = TaskDecomposer(
        enabled=False,
        default_graph_template=GRAPH,
        keystone_harness_id="none",
    )
    plan = decomposer.decompose(task_id="t1", objective="solve")
    assert plan.decomposition_status is DecompositionStatus.DISABLED
    assert plan.subtasks[0].local_graph_template == GRAPH


def test_illegal_candidate_falls_back_explicitly():
    decomposer = TaskDecomposer(
        enabled=True,
        default_graph_template=GRAPH,
        keystone_harness_id="none",
    )
    plan = decomposer.decompose(
        task_id="t1",
        objective="solve",
        candidate_plan={
            "task_id": "t1",
            "subtasks": [
                {
                    "subtask_id": "a",
                    "title": "a",
                    "objective": "x",
                    "dependencies": ["b"],
                    "input_artifacts": [],
                    "expected_outputs": [],
                    "keystone_harness_id": "none",
                    "local_graph_template": GRAPH,
                    "budget": {
                        "max_llm_calls": 1,
                        "max_steps": 1,
                        "timeout_seconds": 10,
                    },
                },
                {
                    "subtask_id": "b",
                    "title": "b",
                    "objective": "y",
                    "dependencies": ["a"],
                    "input_artifacts": [],
                    "expected_outputs": [],
                    "keystone_harness_id": "none",
                    "local_graph_template": GRAPH,
                    "budget": {
                        "max_llm_calls": 1,
                        "max_steps": 1,
                        "timeout_seconds": 10,
                    },
                },
            ],
            "final_aggregation": {},
            "communication_plan": {},
            "decomposition_rationale": "bad",
            "plan_version": 1,
        },
    )
    assert plan.decomposition_status is DecompositionStatus.FALLBACK_SINGLE_SUBTASK
    assert "fallback_reason" in plan.metadata
    assert len(plan.subtasks) == 1


def test_valid_candidate_accepted_when_enabled():
    base = build_single_subtask_plan(
        task_id="t1",
        objective="solve",
        local_graph_template=GRAPH,
        keystone_harness_id="none",
        status=DecompositionStatus.OK,
    )
    decomposer = TaskDecomposer(
        enabled=True,
        default_graph_template=GRAPH,
        keystone_harness_id="none",
    )
    plan = decomposer.decompose(
        task_id="t1",
        objective="solve",
        candidate_plan=base,
    )
    assert plan.decomposition_status is DecompositionStatus.OK
    assert len(plan.subtasks) == 1
