"""Deterministic single-subtask fallback for illegal or disabled decomposition."""

from __future__ import annotations

from typing import Any

from orchestra.backends.base import ArtifactRef, OutputContract
from orchestra.communication.aggregation import AggregationSpec
from orchestra.communication.plan import CommunicationPlan
from orchestra.decomposition.schemas import (
    BudgetSpec,
    DecompositionStatus,
    SubtaskSpec,
    TaskPlan,
)


def build_single_subtask_plan(
    *,
    task_id: str,
    objective: str,
    local_graph_template: str,
    keystone_harness_id: str = "public_code_harness",
    title: str = "solve_task",
    budget: BudgetSpec | None = None,
    plan_version: int = 1,
    rationale: str = "deterministic single-subtask fallback",
    status: DecompositionStatus = DecompositionStatus.FALLBACK_SINGLE_SUBTASK,
    input_artifact_type: str = "ProblemArtifact",
    output_schema: str = "FinalCodeArtifact",
    parser_id: str = "python_code",
    metadata: dict[str, Any] | None = None,
) -> TaskPlan:
    """Build a one-subtask TaskPlan wrapping the original task objective."""
    subtask = SubtaskSpec(
        subtask_id="main",
        title=title,
        objective=objective,
        dependencies=[],
        input_artifacts=[
            ArtifactRef(
                slot="problem",
                artifact_id="",
                artifact_type=input_artifact_type,
            )
        ],
        expected_outputs=[
            OutputContract(
                parser_id=parser_id,
                output_schema=output_schema,
            )
        ],
        keystone_harness_id=keystone_harness_id,
        local_graph_template=local_graph_template,
        budget=budget or BudgetSpec(),
        priority=0,
        metadata={},
    )
    return TaskPlan(
        task_id=task_id,
        subtasks=[subtask],
        final_aggregation=AggregationSpec(
            strategy="identity",
            terminal_subtask_id="main",
        ),
        communication_plan=CommunicationPlan(version=1),
        decomposition_rationale=rationale,
        plan_version=plan_version,
        decomposition_status=status,
        metadata=dict(metadata or {}),
    )
