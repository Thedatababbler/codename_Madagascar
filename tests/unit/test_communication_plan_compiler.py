"""CommunicationPlanCompiler validation and indexing."""

from __future__ import annotations

import pytest

from orchestra.communication.compiler import CommunicationPlanCompiler
from orchestra.communication.payload import DeliveryRule, PayloadContract
from orchestra.communication.plan import CommunicationPlan
from orchestra.communication.validation import (
    CommunicationPlanValidationError,
    CommunicationValidationMode,
)
from orchestra.control.task_state import SubtaskStatus, TaskExecutionState
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
            SubtaskSpec(
                subtask_id="s3",
                title="s3",
                objective="s3",
                dependencies=["s2"],
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


def test_active_execution_allows_historical_completed_target_contract():
    task = _plan()
    comm = CommunicationPlan(
        version=1,
        payload_contracts=[
            PayloadContract(
                payload_id="p1",
                source_subtask_id="s1",
                target_subtask_id="s2",
                artifact_type="FinalAnswerArtifact",
            ),
            PayloadContract(
                payload_id="p2",
                source_subtask_id="s2",
                target_subtask_id="s3",
                artifact_type="FinalAnswerArtifact",
            ),
        ],
        delivery_schedule=[
            DeliveryRule(rule_id="d1", payload_id="p1"),
            DeliveryRule(rule_id="d2", payload_id="p2"),
        ],
    )
    # Historical S1→S2 remains in the active plan after S2 completes.
    compiled = CommunicationPlanCompiler().compile(
        task_plan=task,
        communication_plan=comm,
        target_subtask_id="s3",
        validation_mode=CommunicationValidationMode.ACTIVE_EXECUTION,
    )
    assert list(compiled.payloads_by_id) == ["p2"]
    assert "p1" not in compiled.payloads_by_id
    assert compiled.plan_hash == CommunicationPlanCompiler().compile(
        task_plan=task, communication_plan=comm
    ).plan_hash


def test_proposed_revision_rejects_new_contract_to_completed_target():
    task = _plan()
    state = TaskExecutionState.from_plan(task)
    state.subtasks["s2"].status = SubtaskStatus.COMMITTED
    parent = CommunicationPlan(version=1, payload_contracts=[])
    state.communication_plan = parent
    new_comm = CommunicationPlan(
        version=2,
        payload_contracts=[
            PayloadContract(
                payload_id="new_to_past",
                source_subtask_id="s1",
                target_subtask_id="s2",
                artifact_type="FinalAnswerArtifact",
            )
        ],
        delivery_schedule=[DeliveryRule(rule_id="d", payload_id="new_to_past")],
    )
    with pytest.raises(
        CommunicationPlanValidationError, match="IMMUTABLE_COMMUNICATION_HISTORY"
    ):
        CommunicationPlanCompiler().compile(
            task_plan=task,
            communication_plan=new_comm,
            validation_mode=CommunicationValidationMode.PROPOSED_REVISION,
            current_state=state,
            parent_communication_plan=parent,
        )


def test_active_runtime_compiler_scopes_to_current_target():
    task = _plan()
    comm = CommunicationPlan(
        version=1,
        payload_contracts=[
            PayloadContract(
                payload_id="p1",
                source_subtask_id="s1",
                target_subtask_id="s2",
                artifact_type="FinalAnswerArtifact",
            ),
            PayloadContract(
                payload_id="p2",
                source_subtask_id="s2",
                target_subtask_id="s3",
                artifact_type="FinalAnswerArtifact",
            ),
        ],
        delivery_schedule=[
            DeliveryRule(rule_id="d1", payload_id="p1"),
            DeliveryRule(rule_id="d2", payload_id="p2"),
        ],
    )
    compiled = CommunicationPlanCompiler().compile(
        task_plan=task,
        communication_plan=comm,
        target_subtask_id="s2",
        validation_mode=CommunicationValidationMode.ACTIVE_EXECUTION,
    )
    assert set(compiled.payloads_by_id) == {"p1"}
    assert set(compiled.delivery_rules_by_payload) == {"p1"}
