"""M6 candidate generation unit tests."""

from __future__ import annotations

from orchestra.communication.payload import DeliveryRule, PayloadContract
from orchestra.communication.plan import CommunicationPlan
from orchestra.control.pareto.candidate_generator import ParetoCandidateGenerator
from orchestra.control.pareto.context import build_decision_context
from orchestra.control.pareto.schemas import ParetoConfig, PreferenceProfile
from orchestra.control.slow_loop.schemas import (
    GlobalDiagnosis,
    GlobalDiagnosisReason,
    GlobalObservation,
    TaskSchedulingPolicy,
)
from orchestra.control.task_state import SubtaskStatus, TaskExecutionState
from orchestra.decomposition.schemas import BudgetSpec, SubtaskSpec, TaskPlan

GRAPH = "configs/graphs/codex_single_implementer.yaml"


def _plan() -> TaskPlan:
    return TaskPlan(
        task_id="m6gen",
        plan_version=1,
        decomposition_rationale="m6",
        subtasks=[
            SubtaskSpec(
                subtask_id="s1",
                title="s1",
                objective="a",
                dependencies=[],
                keystone_harness_id="repository_test_harness",
                local_graph_template=GRAPH,
                budget=BudgetSpec(),
            ),
            SubtaskSpec(
                subtask_id="s2",
                title="s2",
                objective="b",
                dependencies=["s1"],
                keystone_harness_id="repository_test_harness",
                local_graph_template=GRAPH,
                budget=BudgetSpec(),
            ),
            SubtaskSpec(
                subtask_id="s3",
                title="s3",
                objective="c",
                dependencies=["s2"],
                keystone_harness_id="repository_test_harness",
                local_graph_template=GRAPH,
                budget=BudgetSpec(),
            ),
        ],
        communication_plan=CommunicationPlan(
            version=1,
            payload_contracts=[
                PayloadContract(
                    payload_id="p12",
                    source_subtask_id="s1",
                    target_subtask_id="s2",
                    artifact_type="FinalAnswerArtifact",
                    required=False,
                    max_tokens=2048,
                    metadata={"slot": "comm:p12"},
                ),
                PayloadContract(
                    payload_id="p23",
                    source_subtask_id="s2",
                    target_subtask_id="s3",
                    artifact_type="FinalAnswerArtifact",
                    required=False,
                    max_tokens=2048,
                    metadata={"slot": "comm:p23"},
                ),
            ],
            delivery_schedule=[
                DeliveryRule(rule_id="r12", payload_id="p12", enabled=True),
                DeliveryRule(rule_id="r23", payload_id="p23", enabled=True),
            ],
            context_budgets={"s2": 4000, "s3": 4000},
        ),
        metadata={
            "backend_capabilities": {"coding": ["codex_sdk", "smolagents_code"]},
        },
    )


def _setup():
    plan = _plan()
    state = TaskExecutionState.from_plan(plan)
    state.subtasks["s1"].status = SubtaskStatus.COMMITTED
    state.subtasks["s2"].status = SubtaskStatus.PENDING
    state.subtasks["s3"].status = SubtaskStatus.PENDING
    eligible = {"s2", "s3"}
    ctx = build_decision_context(
        parent_plan_hash=plan.content_hash(),
        parent_communication_hash=f"comm-v{plan.communication_plan.version}",
        committed_prefix=["s1"],
        eligible_future_subtask_ids=sorted(eligible),
        triggers=["budget_pressure"],
        diagnosis=GlobalDiagnosis(
            reasons=[GlobalDiagnosisReason.BUDGET_PRESSURE],
            update_required=True,
        ),
        preference_profile=PreferenceProfile(profile_id="balanced_knee"),
        backend_capabilities=plan.metadata.get("backend_capabilities", {}),
    )
    obs = GlobalObservation(
        task_id=plan.task_id,
        state_version=0,
        active_plan_version=1,
        active_communication_version=1,
    )
    return plan, state, eligible, ctx, obs


def test_candidate_generation_is_bounded():
    plan, state, eligible, ctx, obs = _setup()
    gen = ParetoCandidateGenerator(ParetoConfig(max_candidates=8))
    cands = gen.generate(
        task_plan=plan,
        communication_plan=plan.communication_plan,
        scheduling_policy=TaskSchedulingPolicy(max_concurrent_subtasks=2),
        state=state,
        observation=obs,
        diagnosis=GlobalDiagnosis(
            reasons=[GlobalDiagnosisReason.BUDGET_PRESSURE], update_required=True
        ),
        eligible=eligible,
        context=ctx,
    )
    assert len(cands) <= 8
    assert len(cands) >= 1


def test_candidate_generation_is_seed_deterministic():
    plan, state, eligible, ctx, obs = _setup()
    gen = ParetoCandidateGenerator(ParetoConfig(max_candidates=8))
    kwargs = dict(
        task_plan=plan,
        communication_plan=plan.communication_plan,
        scheduling_policy=TaskSchedulingPolicy(max_concurrent_subtasks=2),
        state=state,
        observation=obs,
        diagnosis=GlobalDiagnosis(
            reasons=[GlobalDiagnosisReason.BUDGET_PRESSURE], update_required=True
        ),
        eligible=eligible,
        context=ctx,
    )
    a = [c.content_hash for c in gen.generate(**kwargs)]
    b = [c.content_hash for c in gen.generate(**kwargs)]
    assert a == b


def test_candidates_are_future_only():
    plan, state, eligible, ctx, obs = _setup()
    gen = ParetoCandidateGenerator()
    cands = gen.generate(
        task_plan=plan,
        communication_plan=plan.communication_plan,
        scheduling_policy=TaskSchedulingPolicy(),
        state=state,
        observation=obs,
        diagnosis=GlobalDiagnosis(
            reasons=[GlobalDiagnosisReason.CONTEXT_PRESSURE], update_required=True
        ),
        eligible=eligible,
        context=ctx,
    )
    for cand in cands:
        gc = cand.global_candidate
        ids = {s.subtask_id for s in gc.proposed_task_plan.subtasks}
        assert ids == {"s1", "s2", "s3"}
        # s1 stays committed path — edits should not rewrite dependencies of s1.
        s1 = next(s for s in gc.proposed_task_plan.subtasks if s.subtask_id == "s1")
        assert s1.dependencies == []


def test_candidates_do_not_add_subtasks():
    plan, state, eligible, ctx, obs = _setup()
    gen = ParetoCandidateGenerator()
    cands = gen.generate(
        task_plan=plan,
        communication_plan=plan.communication_plan,
        scheduling_policy=TaskSchedulingPolicy(),
        state=state,
        observation=obs,
        diagnosis=GlobalDiagnosis(
            reasons=[GlobalDiagnosisReason.BUDGET_PRESSURE], update_required=True
        ),
        eligible=eligible,
        context=ctx,
    )
    for cand in cands:
        assert len(cand.global_candidate.proposed_task_plan.subtasks) == 3


def test_candidates_do_not_rewire_dependencies():
    plan, state, eligible, ctx, obs = _setup()
    gen = ParetoCandidateGenerator()
    cands = gen.generate(
        task_plan=plan,
        communication_plan=plan.communication_plan,
        scheduling_policy=TaskSchedulingPolicy(),
        state=state,
        observation=obs,
        diagnosis=GlobalDiagnosis(
            reasons=[GlobalDiagnosisReason.SCHEDULING_CONTENTION], update_required=True
        ),
        eligible=eligible,
        context=ctx,
    )
    for cand in cands:
        by_id = {s.subtask_id: s for s in cand.global_candidate.proposed_task_plan.subtasks}
        assert by_id["s2"].dependencies == ["s1"]
        assert by_id["s3"].dependencies == ["s2"]


def test_backend_candidates_use_real_node_ids():
    plan, state, eligible, ctx, obs = _setup()
    # Ensure allowlisted backend pools exist on slow-loop-like metadata.
    plan = plan.model_copy(
        update={
            "metadata": {
                **plan.metadata,
                "allowed_backend_assignments": {
                    "s2": ["codex_sdk"],
                    "s3": ["codex_sdk"],
                },
            }
        }
    )
    gen = ParetoCandidateGenerator()
    cands = gen.generate(
        task_plan=plan,
        communication_plan=plan.communication_plan,
        scheduling_policy=TaskSchedulingPolicy(),
        state=state,
        observation=obs,
        diagnosis=GlobalDiagnosis(
            reasons=[GlobalDiagnosisReason.BACKEND_INSTABILITY], update_required=True
        ),
        eligible=eligible,
        context=ctx,
    )
    for cand in cands:
        for edit in cand.edits:
            if getattr(edit, "type", None) == "pending_backend_assignment":
                assert edit.node_id != "__future_agent__"
                assert edit.node_id


def test_candidate_deduplication():
    plan, state, eligible, ctx, obs = _setup()
    gen = ParetoCandidateGenerator()
    cands = gen.generate(
        task_plan=plan,
        communication_plan=plan.communication_plan,
        scheduling_policy=TaskSchedulingPolicy(),
        state=state,
        observation=obs,
        diagnosis=GlobalDiagnosis(
            reasons=[GlobalDiagnosisReason.BUDGET_PRESSURE], update_required=True
        ),
        eligible=eligible,
        context=ctx,
    )
    hashes = [c.content_hash for c in cands]
    assert len(hashes) == len(set(hashes))


def test_two_edit_compatibility():
    plan, state, eligible, ctx, obs = _setup()
    gen = ParetoCandidateGenerator(ParetoConfig(allow_two_edit_pairs=True, max_candidates=8))
    cands = gen.generate(
        task_plan=plan,
        communication_plan=plan.communication_plan,
        scheduling_policy=TaskSchedulingPolicy(max_concurrent_subtasks=2),
        state=state,
        observation=obs,
        diagnosis=GlobalDiagnosis(
            reasons=[
                GlobalDiagnosisReason.BUDGET_PRESSURE,
                GlobalDiagnosisReason.CONTEXT_PRESSURE,
            ],
            update_required=True,
        ),
        eligible=eligible,
        context=ctx,
    )
    assert any(len(c.edits) >= 1 for c in cands)
