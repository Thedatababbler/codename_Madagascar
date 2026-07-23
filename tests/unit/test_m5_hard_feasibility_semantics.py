"""Hard-feasibility: enabled DeliveryRule semantics + target-local budget repair."""

from __future__ import annotations

from orchestra.communication.ledger import DeliveryFailureReason
from orchestra.communication.payload import DeliveryRule, PayloadContract
from orchestra.communication.plan import CommunicationPlan
from orchestra.control.pareto.candidate import build_pareto_candidate
from orchestra.control.pareto.context import build_decision_context
from orchestra.control.pareto.schemas import ObjectiveDirection, PreferenceProfile
from orchestra.control.pareto.validation import ParetoCandidateValidator
from orchestra.control.slow_loop.candidate_generator import RuleBasedGlobalCandidateGenerator
from orchestra.control.slow_loop.communication_safety import (
    candidate_resolves_active_required_blocks,
    enabled_rule_payload_ids,
    missing_required_rule_payload_ids,
    proposed_comm_resolves_context_budget_infeasible,
    target_communication_semantics,
)
from orchestra.control.slow_loop.edits import apply_global_edits
from orchestra.control.slow_loop.schemas import (
    GlobalCandidate,
    GlobalCandidateValidationStatus,
    GlobalDiagnosis,
    GlobalDiagnosisReason,
    GlobalObservation,
    SchedulingConcurrencyEdit,
    SlowLoopConfig,
    TaskSchedulingPolicy,
    UpsertDeliveryRuleEdit,
    UpsertPayloadContractEdit,
)
from orchestra.control.task_state import SubtaskStatus, TaskExecutionState
from orchestra.decomposition.schemas import BudgetSpec, SubtaskSpec, TaskPlan

GRAPH = "configs/graphs/codex_single_implementer.yaml"
OBJ = {
    "quality": ObjectiveDirection.MAXIMIZE,
    "cost": ObjectiveDirection.MINIMIZE,
}


def _plan(
    *,
    contracts: list[PayloadContract],
    rules: list[DeliveryRule] | None = None,
    budgets: dict[str, int] | None = None,
) -> TaskPlan:
    return TaskPlan(
        task_id="hf",
        plan_version=1,
        decomposition_rationale="hard feasibility",
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
                objective="blocked",
                dependencies=["s1"],
                keystone_harness_id="repository_test_harness",
                local_graph_template=GRAPH,
                budget=BudgetSpec(),
            ),
            SubtaskSpec(
                subtask_id="s3",
                title="s3",
                objective="other",
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
            context_budgets=budgets or {"s2": 1200, "s3": 1200},
        ),
    )


def test_disabled_only_rule_counts_as_missing_enabled_rule():
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
        rules=[DeliveryRule(rule_id="r1", payload_id="p1", enabled=False)],
    )
    assert "p1" not in enabled_rule_payload_ids(plan.communication_plan)
    assert missing_required_rule_payload_ids(
        communication_plan=plan.communication_plan, target_subtask_id="s2"
    ) == ["p1"]


def test_generator_enables_exact_payload_rule_when_only_disabled_present():
    plan = _plan(
        contracts=[
            PayloadContract(
                payload_id="p1",
                source_subtask_id="s1",
                target_subtask_id="s2",
                artifact_type="FinalAnswerArtifact",
                required=True,
                max_tokens=9000,
            ),
            PayloadContract(
                payload_id="opt",
                source_subtask_id="s1",
                target_subtask_id="s2",
                artifact_type="FinalAnswerArtifact",
                required=False,
                max_tokens=4000,
            ),
        ],
        rules=[DeliveryRule(rule_id="r1", payload_id="p1", enabled=False)],
        budgets={"s2": 1200},
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
            task_id="hf",
            state_version=1,
            active_plan_version=1,
            active_communication_version=1,
        ),
        diagnosis=GlobalDiagnosis(
            reasons=[
                GlobalDiagnosisReason.DELIVERY_FAILURE,
                GlobalDiagnosisReason.MISSING_PAYLOAD,
                GlobalDiagnosisReason.CONTEXT_PRESSURE,
            ],
            affected_future_subtask_ids=["s2"],
            update_required=True,
            concise_explanation="disabled rule + pressure",
            recommended_edit_types=[
                "upsert_delivery_rule",
                "upsert_payload_contract",
            ],
        ),
        eligible={"s2", "s3"},
    )
    comm = next(c for c in cands if c.candidate_id == "cand_comm")
    rule_edits = [e for e in comm.edits if isinstance(e, UpsertDeliveryRuleEdit)]
    assert rule_edits
    assert any(e.rule.payload_id == "p1" and e.rule.enabled for e in rule_edits)
    # Prefer replace of the existing disabled rule id.
    assert any(e.rule.rule_id == "r1" and e.rule.enabled for e in rule_edits)
    proposed_enabled = enabled_rule_payload_ids(comm.proposed_communication_plan)
    assert "p1" in proposed_enabled
    ok, _ = candidate_resolves_active_required_blocks(
        state=state,
        proposed_communication_plan=comm.proposed_communication_plan,
    )
    assert ok is True


def test_scheduling_only_rejected_for_context_budget_despite_version_bump():
    plan = _plan(
        contracts=[
            PayloadContract(
                payload_id="big",
                source_subtask_id="s1",
                target_subtask_id="s2",
                artifact_type="FinalAnswerArtifact",
                required=True,
                max_tokens=9000,
            )
        ],
        rules=[DeliveryRule(rule_id="r_big", payload_id="big", enabled=True)],
        budgets={"s2": 1000},
    )
    state = TaskExecutionState.from_plan(plan)
    state.communication_plan = plan.communication_plan
    state.subtasks["s1"].status = SubtaskStatus.COMMITTED
    state.subtasks["s2"].status = SubtaskStatus.READY
    state.subtasks["s2"].communication_block_reason = (
        DeliveryFailureReason.CONTEXT_BUDGET_INFEASIBLE.value
    )
    edits = [SchedulingConcurrencyEdit(max_concurrent_subtasks=1)]
    new_plan, new_comm, new_policy, _ = apply_global_edits(
        task_plan=plan,
        communication_plan=state.communication_plan,
        scheduling_policy=TaskSchedulingPolicy(max_concurrent_subtasks=2),
        edits=edits,
        eligible_subtask_ids={"s2", "s3"},
    )
    assert new_comm.version == state.communication_plan.version + 1
    assert target_communication_semantics(state.communication_plan, "s2") == (
        target_communication_semantics(new_comm, "s2")
    )
    assert not proposed_comm_resolves_context_budget_infeasible(
        current=state.communication_plan,
        proposed=new_comm,
        target_subtask_id="s2",
    )
    ok, errors = candidate_resolves_active_required_blocks(
        state=state, proposed_communication_plan=new_comm
    )
    assert ok is False
    assert any("context_budget_infeasible" in e for e in errors)
    assert any("no target-specific feasible" in e for e in errors)

    g = GlobalCandidate(
        candidate_id="sched-only",
        diagnosis=GlobalDiagnosis(
            reasons=[GlobalDiagnosisReason.CONTEXT_PRESSURE],
            affected_future_subtask_ids=["s2"],
            update_required=True,
            concise_explanation="budget",
        ),
        edits=edits,
        proposed_task_plan=new_plan,
        proposed_communication_plan=new_comm,
        proposed_scheduling_policy=new_policy,
        validation_status=GlobalCandidateValidationStatus.VALID,
    )
    ctx = build_decision_context(
        parent_plan_hash=plan.content_hash(),
        parent_communication_hash="",
        committed_prefix=["s1"],
        eligible_future_subtask_ids={"s2", "s3"},
        triggers=[],
        diagnosis=g.diagnosis,
        preference_profile=PreferenceProfile(),
        backend_capabilities={},
        state=state,
        objective_config=OBJ,
        communication_plan=state.communication_plan,
    )
    pareto_cand = build_pareto_candidate(global_candidate=g, context=ctx)
    result = ParetoCandidateValidator().validate(
        pareto_cand, current_state=state, leased_subtask_ids=set()
    )
    assert result.ok is False


def test_unrelated_target_communication_change_cannot_resolve_block():
    plan = _plan(
        contracts=[
            PayloadContract(
                payload_id="p2",
                source_subtask_id="s1",
                target_subtask_id="s2",
                artifact_type="FinalAnswerArtifact",
                required=True,
                max_tokens=9000,
            ),
            PayloadContract(
                payload_id="p3",
                source_subtask_id="s1",
                target_subtask_id="s3",
                artifact_type="FinalAnswerArtifact",
                required=False,
                max_tokens=4000,
            ),
        ],
        rules=[DeliveryRule(rule_id="r2", payload_id="p2", enabled=True)],
        budgets={"s2": 1000, "s3": 5000},
    )
    state = TaskExecutionState.from_plan(plan)
    state.communication_plan = plan.communication_plan
    state.subtasks["s1"].status = SubtaskStatus.COMMITTED
    state.subtasks["s2"].status = SubtaskStatus.READY
    state.subtasks["s2"].communication_block_reason = (
        DeliveryFailureReason.CONTEXT_BUDGET_INFEASIBLE.value
    )
    # Change only s3's optional contract — must not resolve s2's budget block.
    shrunk = PayloadContract(
        payload_id="p3",
        source_subtask_id="s1",
        target_subtask_id="s3",
        artifact_type="FinalAnswerArtifact",
        required=False,
        max_tokens=64,
    )
    new_plan, new_comm, _, _ = apply_global_edits(
        task_plan=plan,
        communication_plan=state.communication_plan,
        scheduling_policy=TaskSchedulingPolicy(),
        edits=[UpsertPayloadContractEdit(contract=shrunk)],
        eligible_subtask_ids={"s2", "s3"},
    )
    assert new_comm.version > state.communication_plan.version
    assert target_communication_semantics(state.communication_plan, "s2") == (
        target_communication_semantics(new_comm, "s2")
    )
    ok, errors = candidate_resolves_active_required_blocks(
        state=state, proposed_communication_plan=new_comm
    )
    assert ok is False
    assert any("no target-specific feasible" in e for e in errors)
