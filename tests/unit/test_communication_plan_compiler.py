"""CommunicationPlanCompiler validation and indexing."""

from __future__ import annotations

import pytest

from orchestra.communication.compiler import CommunicationPlanCompiler
from orchestra.communication.payload import DeliveryRule, PayloadContract
from orchestra.communication.plan import CommunicationPlan
from orchestra.communication.validation import CommunicationPlanValidationError
from orchestra.decomposition.schemas import BudgetSpec, SubtaskSpec, TaskPlan


def _plan() -> TaskPlan:
    return TaskPlan(
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
                local_graph_template="configs/graphs/codex_single_implementer.yaml",
                budget=BudgetSpec(),
                expected_outputs=[],
            ),
            SubtaskSpec(
                subtask_id="s2",
                title="s2",
                objective="s2",
                dependencies=["s1"],
                keystone_harness_id="h",
                local_graph_template="configs/graphs/codex_single_implementer.yaml",
                budget=BudgetSpec(),
                expected_outputs=[],
            ),
        ],
    )


def test_compiler_indexes_payloads_by_target():
    task = _plan()
    comm = CommunicationPlan(
        version=1,
        payload_contracts=[
            PayloadContract(
                payload_id="p1",
                source_subtask_id="s1",
                target_subtask_id="s2",
                artifact_type="FinalAnswerArtifact",
                max_tokens=128,
            )
        ],
        delivery_schedule=[
            DeliveryRule(rule_id="d1", payload_id="p1", forward_only=True)
        ],
        context_budgets={"s2": 1000},
    )
    compiled = CommunicationPlanCompiler().compile(
        task_plan=task, communication_plan=comm
    )
    assert "s2" in compiled.payloads_by_target
    assert compiled.payloads_by_id["p1"].payload_id == "p1"
    assert compiled.plan_hash


def test_compiler_rejects_private_artifact_type():
    task = _plan()
    comm = CommunicationPlan(
        payload_contracts=[
            PayloadContract(
                payload_id="bad",
                source_subtask_id="s1",
                target_subtask_id="s2",
                artifact_type="PrivateEvaluatorArtifact",
            )
        ]
    )
    with pytest.raises(CommunicationPlanValidationError):
        CommunicationPlanCompiler().compile(task_plan=task, communication_plan=comm)


def test_compiler_rejects_delivery_to_completed():
    task = _plan()
    comm = CommunicationPlan(
        payload_contracts=[
            PayloadContract(
                payload_id="p1",
                source_subtask_id="s1",
                target_subtask_id="s2",
                artifact_type="FinalAnswerArtifact",
            )
        ]
    )
    with pytest.raises(CommunicationPlanValidationError):
        CommunicationPlanCompiler().compile(
            task_plan=task,
            communication_plan=comm,
            completed_subtask_ids={"s2"},
        )
