"""Context budget packing fail-closed on required payloads."""

from __future__ import annotations

import pytest

from orchestra.communication.budget import ContextBudgetError, pack_context_budget
from orchestra.communication.compiler import CommunicationPlanCompiler
from orchestra.communication.payload import PayloadContract
from orchestra.communication.plan import CommunicationPlan
from orchestra.communication.projection import PayloadProjectionResult
from orchestra.decomposition.schemas import BudgetSpec, SubtaskSpec, TaskPlan
from orchestra.ir.artifacts import create_artifact
from orchestra.schemas.artifacts import FinalAnswerArtifact


def test_required_payload_cannot_be_silently_dropped():
    task = TaskPlan(
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
                subtask_id="s2",
                title="s2",
                objective="s2",
                dependencies=["s1"],
                keystone_harness_id="h",
                local_graph_template="g.yaml",
                budget=BudgetSpec(),
                expected_outputs=[],
            ),
        ],
    )
    contract = PayloadContract(
        payload_id="req",
        source_subtask_id="s1",
        target_subtask_id="s2",
        artifact_type="FinalAnswerArtifact",
        max_tokens=1000,
        metadata={"required": True, "priority": 1},
    )
    comm = CommunicationPlan(
        payload_contracts=[contract],
        context_budgets={"s2": 10},
    )
    compiled = CommunicationPlanCompiler().compile(
        task_plan=task, communication_plan=comm
    )
    art = create_artifact(
        FinalAnswerArtifact(answer="big", source_node="s1"),
        producer_node_id="s1",
        task_id="t",
    )
    proj = PayloadProjectionResult(
        payload_id="req",
        source_artifact_id=art.artifact_id,
        projected_artifact=art,
        estimated_tokens=100,
        truncated=False,
    )
    with pytest.raises(ContextBudgetError):
        pack_context_budget(
            target_subtask_id="s2",
            compiled=compiled,
            projections=[proj],
        )
