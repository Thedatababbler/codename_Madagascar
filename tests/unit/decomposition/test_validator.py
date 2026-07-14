"""Unit tests for TaskPlan validation."""

from __future__ import annotations

import pytest

from orchestra.backends.base import ArtifactRef, OutputContract
from orchestra.decomposition.fallback import build_single_subtask_plan
from orchestra.decomposition.schemas import BudgetSpec, DecompositionLimits, SubtaskSpec, TaskPlan
from orchestra.decomposition.validator import TaskPlanValidationError, validate_task_plan

GRAPH = "configs/graphs/b0_direct.yaml"


def _subtask(
    subtask_id: str,
    *,
    deps: list[str] | None = None,
    harness: str = "none",
    graph: str = GRAPH,
) -> SubtaskSpec:
    return SubtaskSpec(
        subtask_id=subtask_id,
        title=subtask_id,
        objective="solve",
        dependencies=list(deps or []),
        input_artifacts=[
            ArtifactRef(slot="problem", artifact_id="", artifact_type="ProblemArtifact")
        ],
        expected_outputs=[
            OutputContract(parser_id="python_code", output_schema="FinalCodeArtifact")
        ],
        keystone_harness_id=harness,
        local_graph_template=graph,
        budget=BudgetSpec(),
    )


def test_valid_single_subtask_plan():
    plan = build_single_subtask_plan(
        task_id="t1",
        objective="echo",
        local_graph_template=GRAPH,
        keystone_harness_id="none",
    )
    validate_task_plan(plan)


def test_cycle_is_rejected():
    plan = TaskPlan(
        task_id="t1",
        subtasks=[
            _subtask("a", deps=["b"]),
            _subtask("b", deps=["a"]),
        ],
    )
    with pytest.raises(TaskPlanValidationError, match="cyclic"):
        validate_task_plan(plan)


def test_missing_dependency_rejected():
    plan = TaskPlan(
        task_id="t1",
        subtasks=[_subtask("a", deps=["missing"])],
    )
    with pytest.raises(TaskPlanValidationError, match="missing"):
        validate_task_plan(plan)


def test_duplicate_ids_rejected():
    plan = TaskPlan(
        task_id="t1",
        subtasks=[_subtask("a"), _subtask("a")],
    )
    with pytest.raises(TaskPlanValidationError, match="duplicate"):
        validate_task_plan(plan)


def test_too_many_subtasks_rejected():
    plan = TaskPlan(
        task_id="t1",
        subtasks=[_subtask(f"s{i}") for i in range(3)],
    )
    with pytest.raises(TaskPlanValidationError, match="too many"):
        validate_task_plan(plan, limits=DecompositionLimits(max_subtasks=2))


def test_unknown_harness_rejected():
    plan = TaskPlan(task_id="t1", subtasks=[_subtask("a", harness="not_a_harness")])
    with pytest.raises(TaskPlanValidationError, match="unknown harness"):
        validate_task_plan(plan)


def test_missing_graph_file_rejected():
    plan = TaskPlan(
        task_id="t1",
        subtasks=[_subtask("a", graph="configs/graphs/does_not_exist.yaml")],
    )
    with pytest.raises(TaskPlanValidationError, match="not found"):
        validate_task_plan(plan, require_graph_files=True)


def test_illegal_dag_no_source():
    # Mutual cycle already covered; self-cycle also yields no source after filter.
    plan = TaskPlan(task_id="t1", subtasks=[_subtask("a", deps=["a"])])
    with pytest.raises(TaskPlanValidationError):
        validate_task_plan(plan)
