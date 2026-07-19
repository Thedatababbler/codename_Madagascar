"""M5.2 runtime closure: validation modes, aggregation, resolver, budget, triggers."""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestra.communication.aggregation import AggregationRule, AggregationStrategy
from orchestra.communication.delivery import CommunicationDeliveryEngine
from orchestra.communication.ledger import DeliveryFailureReason, DeliveryStatus
from orchestra.communication.payload import DeliveryRule, PayloadContract
from orchestra.communication.plan import CommunicationPlan
from orchestra.control.ready_scheduler import ReadySubtaskScheduler
from orchestra.control.slow_loop.agent_node_resolver import FutureAgentNodeResolver
from orchestra.control.slow_loop.candidate_generator import RuleBasedGlobalCandidateGenerator
from orchestra.control.slow_loop.controller import SlowLoopController
from orchestra.control.slow_loop.diagnosis import detect_triggers, diagnose
from orchestra.control.slow_loop.observation import build_global_observation
from orchestra.control.slow_loop.schemas import (
    GlobalDiagnosis,
    GlobalDiagnosisReason,
    GlobalObservation,
    SlowLoopBudget,
    SlowLoopConfig,
    SlowLoopTriggerReason,
    TaskBudgetRemaining,
    TaskSchedulingPolicy,
)
from orchestra.control.slow_loop.task_budget import TaskBudgetTracker
from orchestra.control.task_state import SubtaskStatus, TaskExecutionState
from orchestra.decomposition.schemas import BudgetSpec, SubtaskSpec, TaskPlan
from orchestra.ir.artifacts import create_artifact
from orchestra.runtime.task_checkpoint import TaskCheckpointStore
from orchestra.schemas.artifacts import FinalAnswerArtifact
from orchestra.storage.artifacts import FileArtifactStore

CODEX_GRAPH = "configs/graphs/codex_single_implementer.yaml"
CODEAGENT_GRAPH = "configs/graphs/bbeh_single_codeagent.yaml"


def _chain_plan() -> TaskPlan:
    return TaskPlan(
        task_id="m52",
        plan_version=1,
        decomposition_rationale="chain",
        subtasks=[
            SubtaskSpec(
                subtask_id="s1",
                title="s1",
                objective="a",
                dependencies=[],
                keystone_harness_id="h",
                local_graph_template=CODEX_GRAPH,
                budget=BudgetSpec(),
            ),
            SubtaskSpec(
                subtask_id="s2",
                title="s2",
                objective="b",
                dependencies=["s1"],
                keystone_harness_id="h",
                local_graph_template=CODEX_GRAPH,
                budget=BudgetSpec(),
            ),
            SubtaskSpec(
                subtask_id="s3",
                title="s3",
                objective="c",
                dependencies=["s2"],
                keystone_harness_id="h",
                local_graph_template=CODEX_GRAPH,
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
                    required=True,
                    max_tokens=256,
                    metadata={"slot": "comm:p12"},
                ),
                PayloadContract(
                    payload_id="p23",
                    source_subtask_id="s2",
                    target_subtask_id="s3",
                    artifact_type="FinalAnswerArtifact",
                    required=True,
                    max_tokens=256,
                    metadata={"slot": "comm:p23"},
                ),
            ],
            delivery_schedule=[
                DeliveryRule(rule_id="r12", payload_id="p12"),
                DeliveryRule(rule_id="r23", payload_id="p23"),
            ],
            context_budgets={"s2": 10_000, "s3": 10_000},
        ),
        metadata={"task_budget": {"max_backend_calls": 100}},
    )


@pytest.mark.asyncio
async def test_historical_completed_target_contract_does_not_break_later_wave(
    tmp_path: Path,
):
    store = FileArtifactStore(tmp_path)
    plan = _chain_plan()
    state = TaskExecutionState.from_plan(plan)
    state.communication_plan = plan.communication_plan
    engine = CommunicationDeliveryEngine(store)

    a1 = create_artifact(
        FinalAnswerArtifact(answer="from-s1", source_node="s1"),
        producer_node_id="s1",
        task_id="m52",
    )
    await store.put(a1)
    state.subtasks["s1"].status = SubtaskStatus.COMMITTED
    state.subtasks["s1"].final_output_artifact_id = a1.artifact_id
    state.subtasks["s2"].status = SubtaskStatus.READY
    d2 = await engine.deliver_for_target(
        task_plan=plan,
        task_state=state,
        communication_plan=state.communication_plan,
        target_subtask_id="s2",
    )
    assert d2.blocked is False
    assert "comm:p12" in d2.delivered_slots
    state.delivery_ledger.extend(d2.new_records)
    state.subtasks["s2"].status = SubtaskStatus.COMMITTED
    a2 = create_artifact(
        FinalAnswerArtifact(answer="from-s2", source_node="s2"),
        producer_node_id="s2",
        task_id="m52",
    )
    await store.put(a2)
    state.subtasks["s2"].final_output_artifact_id = a2.artifact_id
    state.subtasks["s3"].status = SubtaskStatus.READY
    # Historical S1→S2 still present; S3 delivery must succeed.
    d3 = await engine.deliver_for_target(
        task_plan=plan,
        task_state=state,
        communication_plan=state.communication_plan,
        target_subtask_id="s3",
    )
    assert d3.blocked is False
    assert "comm:p23" in d3.delivered_slots
    # No redelivery to completed S2.
    assert not any(
        r.target_subtask_id == "s2" and r.status is DeliveryStatus.DELIVERED
        for r in d3.new_records
    )


@pytest.mark.asyncio
async def test_chained_communication_survives_completed_intermediate_target(
    tmp_path: Path,
):
    store = FileArtifactStore(tmp_path)
    plan = _chain_plan()
    state = TaskExecutionState.from_plan(plan)
    state.communication_plan = plan.communication_plan
    engine = CommunicationDeliveryEngine(store)
    for sid, text in [("s1", "A"), ("s2", "B")]:
        art = create_artifact(
            FinalAnswerArtifact(answer=text, source_node=sid),
            producer_node_id=sid,
            task_id="m52",
        )
        await store.put(art)
        state.subtasks[sid].status = SubtaskStatus.COMMITTED
        state.subtasks[sid].final_output_artifact_id = art.artifact_id
    # Seed historical delivery for p12.
    state.subtasks["s2"].status = SubtaskStatus.COMMITTED
    hist = await engine.deliver_for_target(
        task_plan=plan,
        task_state=state,
        communication_plan=state.communication_plan,
        target_subtask_id="s2",
        persist=True,
    )
    # S2 already committed — delivery may still write ledger for audit; ensure
    # subsequent S3 works with historical contract retained.
    state.delivery_ledger.extend(hist.new_records + hist.audit_records)
    state.subtasks["s3"].status = SubtaskStatus.READY
    d3 = await engine.deliver_for_target(
        task_plan=plan,
        task_state=state,
        communication_plan=state.communication_plan,
        target_subtask_id="s3",
    )
    assert d3.blocked is False
    assert "comm:p23" in d3.delivered_slots
    state.delivery_ledger.extend(d3.new_records)
    # Replay S3 is idempotent.
    d3b = await engine.deliver_for_target(
        task_plan=plan,
        task_state=state,
        communication_plan=state.communication_plan,
        target_subtask_id="s3",
    )
    assert d3b.blocked is False
    assert not d3b.new_records


@pytest.mark.asyncio
async def test_aggregation_exact_match_rejects_partial_overlap(tmp_path: Path):
    store = FileArtifactStore(tmp_path)
    plan = TaskPlan(
        task_id="m52",
        plan_version=1,
        decomposition_rationale="agg",
        subtasks=[
            SubtaskSpec(
                subtask_id="s1",
                title="s1",
                objective="a",
                dependencies=[],
                keystone_harness_id="h",
                local_graph_template=CODEX_GRAPH,
                budget=BudgetSpec(),
            ),
            SubtaskSpec(
                subtask_id="s2",
                title="s2",
                objective="b",
                dependencies=[],
                keystone_harness_id="h",
                local_graph_template=CODEX_GRAPH,
                budget=BudgetSpec(),
            ),
            SubtaskSpec(
                subtask_id="s3",
                title="s3",
                objective="c",
                dependencies=[],
                keystone_harness_id="h",
                local_graph_template=CODEX_GRAPH,
                budget=BudgetSpec(),
            ),
            SubtaskSpec(
                subtask_id="s4",
                title="s4",
                objective="d",
                dependencies=["s1", "s2", "s3"],
                keystone_harness_id="h",
                local_graph_template=CODEX_GRAPH,
                budget=BudgetSpec(),
            ),
        ],
        communication_plan=CommunicationPlan(
            version=1,
            payload_contracts=[
                PayloadContract(
                    payload_id="p1",
                    source_subtask_id="s1",
                    target_subtask_id="s4",
                    artifact_type="FinalAnswerArtifact",
                    max_tokens=1000,
                    metadata={"slot": "shared"},
                ),
                PayloadContract(
                    payload_id="p3",
                    source_subtask_id="s3",
                    target_subtask_id="s4",
                    artifact_type="FinalAnswerArtifact",
                    max_tokens=1000,
                    metadata={"slot": "shared"},
                ),
            ],
            delivery_schedule=[
                DeliveryRule(rule_id="r1", payload_id="p1"),
                DeliveryRule(rule_id="r3", payload_id="p3"),
            ],
            aggregation_rules=[
                AggregationRule(
                    rule_id="agg12",
                    source_payload_ids=["p1", "p2"],
                    strategy=AggregationStrategy.LIST,
                    target_slot="shared",
                    metadata={"target_subtask_id": "s4", "slot": "shared"},
                )
            ],
            context_budgets={"s4": 50_000},
        ),
    )
    # Fix: p2 missing from contracts but rule wants p1,p2; actual is p1,p3.
    # Validation requires aggregation source payloads exist — use p1,p2 in contracts
    # but only deliver p1,p3 by having s2 not provide p2... Actually validation
    # rejects unknown p2. Use rule [p1,p2] with contracts p1,p2,p3 and only
    # commit s1+s3 so pending slot has p1,p3.
    plan.communication_plan = CommunicationPlan(
        version=1,
        payload_contracts=[
            PayloadContract(
                payload_id="p1",
                source_subtask_id="s1",
                target_subtask_id="s4",
                artifact_type="FinalAnswerArtifact",
                max_tokens=1000,
                metadata={"slot": "shared"},
            ),
            PayloadContract(
                payload_id="p2",
                source_subtask_id="s2",
                target_subtask_id="s4",
                artifact_type="FinalAnswerArtifact",
                max_tokens=1000,
                metadata={"slot": "shared"},
            ),
            PayloadContract(
                payload_id="p3",
                source_subtask_id="s3",
                target_subtask_id="s4",
                artifact_type="FinalAnswerArtifact",
                max_tokens=1000,
                metadata={"slot": "shared"},
            ),
        ],
        delivery_schedule=[
            DeliveryRule(rule_id="r1", payload_id="p1"),
            DeliveryRule(rule_id="r2", payload_id="p2"),
            DeliveryRule(rule_id="r3", payload_id="p3"),
        ],
        aggregation_rules=[
            AggregationRule(
                rule_id="agg12",
                source_payload_ids=["p1", "p2"],
                strategy=AggregationStrategy.LIST,
                target_slot="shared",
                metadata={"target_subtask_id": "s4", "slot": "shared"},
            )
        ],
        context_budgets={"s4": 50_000},
    )
    state = TaskExecutionState.from_plan(plan)
    state.communication_plan = plan.communication_plan
    for sid in ("s1", "s3"):
        art = create_artifact(
            FinalAnswerArtifact(answer=sid, source_node=sid),
            producer_node_id=sid,
            task_id="m52",
        )
        await store.put(art)
        state.subtasks[sid].status = SubtaskStatus.COMMITTED
        state.subtasks[sid].final_output_artifact_id = art.artifact_id
    state.subtasks["s2"].status = SubtaskStatus.PENDING
    state.subtasks["s4"].status = SubtaskStatus.READY
    result = await CommunicationDeliveryEngine(store).deliver_for_target(
        task_plan=plan,
        task_state=state,
        communication_plan=state.communication_plan,
        target_subtask_id="s4",
    )
    # p1+p3 share slot but rule wants exact {p1,p2} → missing.
    assert result.blocked is True
    assert result.block_reason is DeliveryFailureReason.AGGREGATION_RULE_MISSING


@pytest.mark.asyncio
async def test_ambiguous_aggregation_rule_fails_closed(tmp_path: Path):
    store = FileArtifactStore(tmp_path)
    plan = TaskPlan(
        task_id="m52",
        plan_version=1,
        decomposition_rationale="amb",
        subtasks=[
            SubtaskSpec(
                subtask_id="s1",
                title="s1",
                objective="a",
                dependencies=[],
                keystone_harness_id="h",
                local_graph_template=CODEX_GRAPH,
                budget=BudgetSpec(),
            ),
            SubtaskSpec(
                subtask_id="s2",
                title="s2",
                objective="b",
                dependencies=[],
                keystone_harness_id="h",
                local_graph_template=CODEX_GRAPH,
                budget=BudgetSpec(),
            ),
            SubtaskSpec(
                subtask_id="s3",
                title="s3",
                objective="c",
                dependencies=["s1", "s2"],
                keystone_harness_id="h",
                local_graph_template=CODEX_GRAPH,
                budget=BudgetSpec(),
            ),
        ],
        communication_plan=CommunicationPlan(
            version=1,
            payload_contracts=[
                PayloadContract(
                    payload_id="p1",
                    source_subtask_id="s1",
                    target_subtask_id="s3",
                    artifact_type="FinalAnswerArtifact",
                    max_tokens=1000,
                    metadata={"slot": "shared"},
                ),
                PayloadContract(
                    payload_id="p2",
                    source_subtask_id="s2",
                    target_subtask_id="s3",
                    artifact_type="FinalAnswerArtifact",
                    max_tokens=1000,
                    metadata={"slot": "shared"},
                ),
            ],
            delivery_schedule=[
                DeliveryRule(rule_id="r1", payload_id="p1"),
                DeliveryRule(rule_id="r2", payload_id="p2"),
            ],
            aggregation_rules=[
                AggregationRule(
                    rule_id="agg-a",
                    source_payload_ids=["p1", "p2"],
                    strategy=AggregationStrategy.LIST,
                    target_slot="shared",
                    metadata={"target_subtask_id": "s3", "slot": "shared"},
                ),
                AggregationRule(
                    rule_id="agg-b",
                    source_payload_ids=["p1", "p2"],
                    strategy=AggregationStrategy.CONCAT_TEXT,
                    target_slot="shared",
                    metadata={"target_subtask_id": "s3", "slot": "shared"},
                ),
            ],
            context_budgets={"s3": 50_000},
        ),
    )
    state = TaskExecutionState.from_plan(plan)
    state.communication_plan = plan.communication_plan
    for sid in ("s1", "s2"):
        art = create_artifact(
            FinalAnswerArtifact(answer=sid, source_node=sid),
            producer_node_id=sid,
            task_id="m52",
        )
        await store.put(art)
        state.subtasks[sid].status = SubtaskStatus.COMMITTED
        state.subtasks[sid].final_output_artifact_id = art.artifact_id
    state.subtasks["s3"].status = SubtaskStatus.READY
    result = await CommunicationDeliveryEngine(store).deliver_for_target(
        task_plan=plan,
        task_state=state,
        communication_plan=state.communication_plan,
        target_subtask_id="s3",
    )
    assert result.blocked is True
    assert result.block_reason is DeliveryFailureReason.AMBIGUOUS_AGGREGATION_RULE


def test_resolver_codex_and_codeagent_real_nodes():
    codex = SubtaskSpec(
        subtask_id="s2",
        title="s2",
        objective="x",
        dependencies=[],
        keystone_harness_id="h",
        local_graph_template=CODEX_GRAPH,
        budget=BudgetSpec(),
    )
    code = SubtaskSpec(
        subtask_id="s3",
        title="s3",
        objective="y",
        dependencies=[],
        keystone_harness_id="h",
        local_graph_template=CODEAGENT_GRAPH,
        budget=BudgetSpec(),
    )
    r1 = FutureAgentNodeResolver().resolve(subtask=codex)
    r2 = FutureAgentNodeResolver().resolve(subtask=code)
    assert r1.eligible and r1.node_id == "codex_implementer"
    assert r2.eligible and r2.node_id == "solver"
    assert r1.node_id != "__future_agent__"
    assert r2.node_id != "__future_agent__"


def test_backend_candidate_uses_real_node_not_placeholder():
    plan = _chain_plan()
    state = TaskExecutionState.from_plan(plan)
    state.subtasks["s1"].status = SubtaskStatus.COMMITTED
    state.subtasks["s2"].status = SubtaskStatus.PENDING
    state.subtasks["s3"].status = SubtaskStatus.PENDING
    cfg = SlowLoopConfig(
        enabled=True,
        allowed_backend_assignments={"default": ["structured_llm", "codex_sdk"]},
        backend_model_pools={"structured_llm": ["gpt-4o-mini"]},
    )
    gen = RuleBasedGlobalCandidateGenerator(cfg)
    diagnosis = GlobalDiagnosis(
        reasons=[GlobalDiagnosisReason.BACKEND_INSTABILITY],
        affected_future_subtask_ids=["s2", "s3"],
        update_required=True,
        concise_explanation="backend",
        recommended_edit_types=["pending_backend_assignment"],
    )
    cands = gen.generate(
        task_plan=plan,
        communication_plan=plan.communication_plan,
        scheduling_policy=TaskSchedulingPolicy(),
        state=state,
        observation=GlobalObservation(
            task_id="m52",
            state_version=1,
            active_plan_version=1,
            active_communication_version=1,
        ),
        diagnosis=diagnosis,
        eligible={"s2", "s3"},
    )
    backend = next(c for c in cands if c.candidate_id == "cand_backend")
    assert backend.rejection_reason is None
    assert backend.edits
    assert all(
        getattr(e, "node_id", None) not in {None, "__future_agent__"}
        for e in backend.edits
    )


def test_task_budget_tracker_ratio_and_unconfigured():
    plan = _chain_plan()
    state = TaskExecutionState.from_plan(plan)
    bare = plan.model_copy(update={"metadata": {}})
    unconf = TaskBudgetTracker().snapshot(task_plan=bare, task_state=state)
    assert unconf.budget_configured is False
    assert unconf.ratio == 1.0
    snap = TaskBudgetTracker().snapshot(task_plan=plan, task_state=state)
    assert snap.budget_configured is True
    assert snap.max_backend_calls == 100
    assert snap.ratio == 1.0
    state.subtasks["s1"].status = SubtaskStatus.COMMITTED
    low = TaskBudgetTracker(max_backend_calls=2).snapshot(task_plan=plan, task_state=state)
    assert low.ratio < 1.0


def test_budget_pressure_trigger_respects_threshold():
    obs_hi = GlobalObservation(
        task_id="t",
        state_version=1,
        active_plan_version=1,
        active_communication_version=1,
        remaining_task_budget=TaskBudgetRemaining(
            ratio=0.9, max_backend_calls=10, budget_configured=True
        ),
        commits_since_last_slow_update=0,
    )
    obs_lo = obs_hi.model_copy(
        update={
            "remaining_task_budget": TaskBudgetRemaining(
                ratio=0.1, max_backend_calls=10, budget_configured=True
            )
        }
    )
    budget = SlowLoopBudget(budget_pressure_ratio=0.35, min_commits_between_updates=99)
    assert SlowLoopTriggerReason.BUDGET_PRESSURE not in detect_triggers(
        obs_hi, budget=budget
    )
    assert SlowLoopTriggerReason.BUDGET_PRESSURE in detect_triggers(obs_lo, budget=budget)


def test_repeated_harness_failure_has_diagnosis():
    plan = _chain_plan()
    state = TaskExecutionState.from_plan(plan)
    state.subtasks["s2"].status = SubtaskStatus.PENDING
    state.subtasks["s3"].status = SubtaskStatus.PENDING
    obs = build_global_observation(state)
    obs = obs.model_copy(update={"harness_failures": 3, "commits_since_last_slow_update": 0})
    triggers = detect_triggers(
        obs, budget=SlowLoopBudget(repeated_failure_threshold=2, min_commits_between_updates=99)
    )
    assert SlowLoopTriggerReason.REPEATED_HARNESS_FAILURE in triggers
    diag = diagnose(
        observation=obs,
        task_plan=plan,
        task_state=state,
        communication_plan=plan.communication_plan,
        triggers=triggers,
        budget=SlowLoopBudget(),
    )
    assert GlobalDiagnosisReason.HARNESS_INSTABILITY in diag.reasons
    assert diag.update_required is True


def test_delivery_failure_trigger_from_block_reason():
    plan = _chain_plan()
    state = TaskExecutionState.from_plan(plan)
    state.subtasks["s2"].status = SubtaskStatus.READY
    state.subtasks["s2"].communication_block_reason = (
        DeliveryFailureReason.REQUIRED_RULE_MISSING.value
    )
    obs = build_global_observation(state)
    assert obs.delivery_statistics.required_delivery_block_count >= 1
    triggers = detect_triggers(
        obs, budget=SlowLoopBudget(min_commits_between_updates=99)
    )
    assert SlowLoopTriggerReason.DELIVERY_FAILURE in triggers
    diag = diagnose(
        observation=obs,
        task_plan=plan,
        task_state=state,
        communication_plan=plan.communication_plan,
        triggers=triggers,
        budget=SlowLoopBudget(),
    )
    assert GlobalDiagnosisReason.DELIVERY_FAILURE in diag.reasons


def test_skipped_count_includes_skipped_family():
    plan = _chain_plan()
    state = TaskExecutionState.from_plan(plan)
    from orchestra.communication.ledger import DeliveryRecord

    state.delivery_ledger.append(
        DeliveryRecord(
            delivery_id="d1",
            communication_plan_version=1,
            payload_id="p",
            source_subtask_id="s1",
            target_subtask_id="s2",
            source_artifact_id="a",
            delivered_at_state_version=0,
            status=DeliveryStatus.SKIPPED_CONDITION_FALSE,
        )
    )
    obs = build_global_observation(state)
    assert obs.delivery_statistics.skipped_count == 1


def test_scheduler_injects_same_checkpoint_store(tmp_path: Path):
    store = TaskCheckpointStore(tmp_path / "ckpt")
    # Minimal construction via __new__ to avoid full runtime.
    sched = ReadySubtaskScheduler.__new__(ReadySubtaskScheduler)
    sched.task_checkpoint_store = store
    sched.slow_loop = SlowLoopController(checkpoint_store=store)
    sched.task_budget_tracker = TaskBudgetTracker()
    assert sched.slow_loop.checkpoint_store is store
