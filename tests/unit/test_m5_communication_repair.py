"""M5 exact communication repair: missing DeliveryRule on existing contracts."""

from __future__ import annotations

from orchestra.communication.ledger import DeliveryFailureReason
from orchestra.communication.payload import DeliveryRule, PayloadContract
from orchestra.communication.plan import CommunicationPlan
from orchestra.control.slow_loop.candidate_generator import RuleBasedGlobalCandidateGenerator
from orchestra.control.slow_loop.schemas import (
    GlobalDiagnosis,
    GlobalDiagnosisReason,
    GlobalObservation,
    SlowLoopConfig,
    TaskSchedulingPolicy,
    UpsertDeliveryRuleEdit,
    UpsertPayloadContractEdit,
)
from orchestra.control.task_state import SubtaskStatus, TaskExecutionState
from orchestra.decomposition.schemas import BudgetSpec, SubtaskSpec, TaskPlan

GRAPH = "configs/graphs/codex_single_implementer.yaml"


def _plan(*, contracts, rules=None) -> TaskPlan:
    return TaskPlan(
        task_id="m5_repair",
        plan_version=1,
        decomposition_rationale="repair",
        subtasks=[
            SubtaskSpec(
                subtask_id="s1",
                title="s1",
                objective="src",
                dependencies=[],
                keystone_harness_id="repository_test_harness",
                local_graph_template=GRAPH,
                budget=BudgetSpec(),
            ),
            SubtaskSpec(
                subtask_id="s2",
                title="s2",
                objective="tgt",
                dependencies=["s1"],
                keystone_harness_id="repository_test_harness",
                local_graph_template=GRAPH,
                budget=BudgetSpec(),
            ),
        ],
        communication_plan=CommunicationPlan(
            version=1,
            payload_contracts=contracts,
            delivery_schedule=list(rules or []),
        ),
    )


def _diagnosis(affected: list[str] | None = None) -> GlobalDiagnosis:
    return GlobalDiagnosis(
        reasons=[
            GlobalDiagnosisReason.DELIVERY_FAILURE,
            GlobalDiagnosisReason.MISSING_PAYLOAD,
        ],
        affected_future_subtask_ids=affected or ["s2"],
        update_required=True,
        concise_explanation="missing rule",
        recommended_edit_types=["upsert_payload_contract", "upsert_delivery_rule"],
    )


def test_existing_required_contract_missing_rule_is_restored_without_duplicate():
    plan = _plan(
        contracts=[
            PayloadContract(
                payload_id="required_payload",
                source_subtask_id="s1",
                target_subtask_id="s2",
                artifact_type="FinalAnswerArtifact",
                required=True,
            )
        ]
    )
    state = TaskExecutionState.from_plan(plan)
    state.communication_plan = plan.communication_plan
    state.subtasks["s1"].status = SubtaskStatus.COMMITTED
    state.subtasks["s2"].status = SubtaskStatus.READY
    state.subtasks["s2"].communication_block_reason = (
        DeliveryFailureReason.REQUIRED_RULE_MISSING.value
    )
    gen = RuleBasedGlobalCandidateGenerator(SlowLoopConfig(enabled=True))
    cands = gen.generate(
        task_plan=plan,
        communication_plan=plan.communication_plan,
        scheduling_policy=TaskSchedulingPolicy(),
        state=state,
        observation=GlobalObservation(
            task_id="m5_repair",
            state_version=1,
            active_plan_version=1,
            active_communication_version=1,
        ),
        diagnosis=_diagnosis(),
        eligible={"s2"},
    )
    assert cands
    comm = next(c for c in cands if c.candidate_id == "cand_comm")
    rule_edits = [e for e in comm.edits if isinstance(e, UpsertDeliveryRuleEdit)]
    contract_edits = [e for e in comm.edits if isinstance(e, UpsertPayloadContractEdit)]
    assert len(rule_edits) == 1
    assert rule_edits[0].rule.payload_id == "required_payload"
    assert contract_edits == []
    # Proposed plan must keep the original contract and add exactly one rule.
    proposed = comm.proposed_communication_plan
    assert [c.payload_id for c in proposed.payload_contracts] == ["required_payload"]
    assert [r.payload_id for r in proposed.delivery_schedule] == ["required_payload"]


def test_existing_auto_contract_missing_rule_is_restored():
    plan = _plan(
        contracts=[
            PayloadContract(
                payload_id="auto_s1_to_s2",
                source_subtask_id="s1",
                target_subtask_id="s2",
                artifact_type="FinalAnswerArtifact",
                required=True,
                metadata={"required": True, "slot": "comm:auto_s1_to_s2"},
            )
        ]
    )
    state = TaskExecutionState.from_plan(plan)
    state.communication_plan = plan.communication_plan
    state.subtasks["s1"].status = SubtaskStatus.COMMITTED
    state.subtasks["s2"].status = SubtaskStatus.READY
    state.subtasks["s2"].communication_block_reason = (
        DeliveryFailureReason.REQUIRED_RULE_MISSING.value
    )
    gen = RuleBasedGlobalCandidateGenerator(SlowLoopConfig(enabled=True))
    cands = gen.generate(
        task_plan=plan,
        communication_plan=plan.communication_plan,
        scheduling_policy=TaskSchedulingPolicy(),
        state=state,
        observation=GlobalObservation(
            task_id="m5_repair",
            state_version=1,
            active_plan_version=1,
            active_communication_version=1,
        ),
        diagnosis=_diagnosis(),
        eligible={"s2"},
    )
    comm = next(c for c in cands if c.candidate_id == "cand_comm")
    assert [e for e in comm.edits if isinstance(e, UpsertPayloadContractEdit)] == []
    rule_edits = [e for e in comm.edits if isinstance(e, UpsertDeliveryRuleEdit)]
    assert len(rule_edits) == 1
    assert rule_edits[0].rule.payload_id == "auto_s1_to_s2"
    assert len(comm.proposed_communication_plan.payload_contracts) == 1
    assert len(comm.proposed_communication_plan.delivery_schedule) == 1


def test_unrepairable_condition_yields_no_comm_candidate():
    plan = _plan(
        contracts=[
            PayloadContract(
                payload_id="p1",
                source_subtask_id="s1",
                target_subtask_id="s2",
                artifact_type="FinalAnswerArtifact",
                required=True,
            )
        ],
        rules=[DeliveryRule(rule_id="r1", payload_id="p1")],
    )
    state = TaskExecutionState.from_plan(plan)
    state.communication_plan = plan.communication_plan
    state.subtasks["s1"].status = SubtaskStatus.COMMITTED
    state.subtasks["s2"].status = SubtaskStatus.READY
    state.subtasks["s2"].communication_block_reason = (
        DeliveryFailureReason.REQUIRED_CONDITION_UNSATISFIED.value
    )
    gen = RuleBasedGlobalCandidateGenerator(SlowLoopConfig(enabled=True))
    cands = gen.generate(
        task_plan=plan,
        communication_plan=plan.communication_plan,
        scheduling_policy=TaskSchedulingPolicy(),
        state=state,
        observation=GlobalObservation(
            task_id="m5_repair",
            state_version=1,
            active_plan_version=1,
            active_communication_version=1,
        ),
        diagnosis=_diagnosis(),
        eligible={"s2"},
    )
    assert not any(c.candidate_id == "cand_comm" and c.edits for c in cands)
