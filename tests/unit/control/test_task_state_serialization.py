"""TaskExecutionState serialization."""

from __future__ import annotations

from orchestra.control.task_state import SubtaskStatus, TaskExecutionState
from orchestra.decomposition.fallback import build_single_subtask_plan

GRAPH = "configs/graphs/b0_direct.yaml"


def test_from_plan_marks_source_ready():
    plan = build_single_subtask_plan(
        task_id="echo",
        objective="echo",
        local_graph_template=GRAPH,
        keystone_harness_id="none",
    )
    state = TaskExecutionState.from_plan(plan, artifact_store_ref="/tmp/artifacts")
    assert state.subtasks["main"].status is SubtaskStatus.READY
    assert state.plan_content_hash == plan.content_hash()


def test_task_state_json_roundtrip():
    plan = build_single_subtask_plan(
        task_id="echo",
        objective="echo",
        local_graph_template=GRAPH,
        keystone_harness_id="none",
    )
    state = TaskExecutionState.from_plan(plan)
    restored = TaskExecutionState.model_validate_json(state.model_dump_json())
    assert restored.task_id == state.task_id
    assert restored.subtasks["main"].status is SubtaskStatus.READY
    assert restored.task_plan.content_hash() == plan.content_hash()
