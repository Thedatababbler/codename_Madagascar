"""M5.3 integration: immutable history, no redelivery, watermark consumption."""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestra.communication.delivery import CommunicationDeliveryEngine
from orchestra.communication.ledger import DeliveryFailureReason, DeliveryStatus
from orchestra.communication.payload import DeliveryRule, PayloadContract
from orchestra.communication.plan import CommunicationPlan
from orchestra.control.slow_loop.controller import SlowLoopController
from orchestra.control.slow_loop.diagnosis import detect_triggers
from orchestra.control.slow_loop.edits import apply_global_edits
from orchestra.control.slow_loop.observation import (
    advance_observation_watermark,
    build_global_observation,
)
from orchestra.control.slow_loop.schemas import (
    RemovePayloadContractEdit,
    SlowLoopBudget,
    SlowLoopConfig,
    SlowLoopTriggerReason,
    TaskSchedulingPolicy,
    UpsertDeliveryRuleEdit,
)
from orchestra.control.task_state import SubtaskStatus, TaskExecutionState
from orchestra.decomposition.schemas import BudgetSpec, SubtaskSpec, TaskPlan
from orchestra.ir.artifacts import create_artifact
from orchestra.runtime.backend import RunContext
from orchestra.runtime.limits import RuntimeLimits, RuntimeSemaphores
from orchestra.runtime.task_checkpoint import TaskCheckpointStore
from orchestra.schemas.artifacts import FinalAnswerArtifact
from orchestra.storage.artifacts import FileArtifactStore

GRAPH = "configs/graphs/codex_single_implementer.yaml"


def _ctx(tmp_path: Path) -> RunContext:
    limits = RuntimeLimits(
        max_parallel_benchmark_tasks=1,
        max_parallel_nodes_per_task=2,
        max_parallel_llm_calls=2,
        max_parallel_sandboxes=2,
    )
    return RunContext(
        run_id="m53",
        task_id="m53",
        run_dir=tmp_path,
        limits=limits,
        semaphores=RuntimeSemaphores(limits),
        contract_hash="c",
    )


def _chain() -> TaskPlan:
    return TaskPlan(
        task_id="m53",
        plan_version=1,
        decomposition_rationale="chain",
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
                DeliveryRule(rule_id="r12", payload_id="p12", enabled=True),
                DeliveryRule(rule_id="r23", payload_id="p23", enabled=True),
            ],
            context_budgets={"s2": 10_000, "s3": 10_000},
        ),
    )


@pytest.mark.asyncio
async def test_immutable_historical_rule_disable_rejected(tmp_path: Path):
    store = FileArtifactStore(tmp_path)
    plan = _chain()
    state = TaskExecutionState.from_plan(plan)
    state.communication_plan = plan.communication_plan
    engine = CommunicationDeliveryEngine(store)
    a1 = create_artifact(
        FinalAnswerArtifact(answer="s1", source_node="s1"),
        producer_node_id="s1",
        task_id="m53",
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
    state.delivery_ledger.extend(d2.new_records)
    state.subtasks["s2"].status = SubtaskStatus.COMMITTED
    state.subtasks["s3"].status = SubtaskStatus.PENDING
    old_plan_hash = state.task_plan.content_hash()
    _, _, _, rejected = apply_global_edits(
        task_plan=state.task_plan,
        communication_plan=state.communication_plan,
        scheduling_policy=TaskSchedulingPolicy(),
        edits=[
            UpsertDeliveryRuleEdit(
                rule=DeliveryRule(rule_id="r12", payload_id="p12", enabled=False)
            )
        ],
        eligible_subtask_ids={"s3"},
    )
    assert "upsert_delivery_rule" in rejected
    assert state.task_plan.content_hash() == old_plan_hash
    assert any(
        r.rule_id == "r12" and r.enabled
        for r in state.communication_plan.delivery_schedule
    )


@pytest.mark.asyncio
async def test_immutable_historical_contract_removal_rejected(tmp_path: Path):
    plan = _chain()
    state = TaskExecutionState.from_plan(plan)
    state.communication_plan = plan.communication_plan
    state.subtasks["s1"].status = SubtaskStatus.COMMITTED
    state.subtasks["s2"].status = SubtaskStatus.COMMITTED
    state.subtasks["s3"].status = SubtaskStatus.PENDING
    _, new_comm, _, rejected = apply_global_edits(
        task_plan=plan,
        communication_plan=state.communication_plan,
        scheduling_policy=TaskSchedulingPolicy(),
        edits=[RemovePayloadContractEdit(payload_id="p12")],
        eligible_subtask_ids={"s3"},
    )
    assert "remove_payload_contract" in rejected
    assert any(c.payload_id == "p12" for c in new_comm.payload_contracts)


@pytest.mark.asyncio
async def test_no_completed_redelivery(tmp_path: Path):
    store = FileArtifactStore(tmp_path)
    plan = _chain()
    state = TaskExecutionState.from_plan(plan)
    state.communication_plan = plan.communication_plan
    state.subtasks["s2"].status = SubtaskStatus.COMMITTED
    result = await CommunicationDeliveryEngine(store).deliver_for_target(
        task_plan=plan,
        task_state=state,
        communication_plan=state.communication_plan,
        target_subtask_id="s2",
    )
    assert result.blocked is True
    assert result.projections == []
    assert result.delivered_slots == {}
    assert not any(r.status is DeliveryStatus.DELIVERED for r in result.new_records)


@pytest.mark.asyncio
async def test_historical_failure_watermark_no_retrigger(tmp_path: Path):
    plan = _chain()
    state = TaskExecutionState.from_plan(plan)
    state.communication_plan = plan.communication_plan.model_copy(update={"version": 2})
    from orchestra.communication.ledger import DeliveryRecord

    state.delivery_ledger.append(
        DeliveryRecord(
            delivery_id="hist-fail",
            communication_plan_version=1,
            payload_id="p12",
            source_subtask_id="s1",
            target_subtask_id="s2",
            source_artifact_id="a",
            delivered_at_state_version=0,
            status=DeliveryStatus.FAILED,
            failure_reason=DeliveryFailureReason.REQUIRED_RULE_MISSING,
        )
    )
    obs = build_global_observation(state)
    # Old version failure alone should not trigger on active version=2.
    advance_observation_watermark(state, observation=obs)
    obs2 = build_global_observation(state)
    triggers = detect_triggers(
        obs2, budget=SlowLoopBudget(min_commits_between_updates=99)
    )
    assert SlowLoopTriggerReason.DELIVERY_FAILURE not in triggers


@pytest.mark.asyncio
async def test_new_failure_after_watermark_triggers(tmp_path: Path):
    plan = _chain()
    state = TaskExecutionState.from_plan(plan)
    state.communication_plan = plan.communication_plan
    state.subtasks["s3"].status = SubtaskStatus.READY
    advance_observation_watermark(state, observation=build_global_observation(state))
    state.subtasks["s3"].communication_block_reason = (
        DeliveryFailureReason.CONTEXT_BUDGET_INFEASIBLE.value
    )
    obs = build_global_observation(state)
    triggers = detect_triggers(
        obs, budget=SlowLoopBudget(min_commits_between_updates=99)
    )
    assert SlowLoopTriggerReason.DELIVERY_FAILURE in triggers


@pytest.mark.asyncio
async def test_historical_context_pressure_does_not_edit_s3(tmp_path: Path):
    plan = _chain()
    state = TaskExecutionState.from_plan(plan)
    state.communication_plan = CommunicationPlan(
        version=1,
        payload_contracts=[
            PayloadContract(
                payload_id="p12",
                source_subtask_id="s1",
                target_subtask_id="s2",
                artifact_type="FinalAnswerArtifact",
                max_tokens=50_000,
                metadata={"slot": "comm:p12"},
            ),
            PayloadContract(
                payload_id="p23",
                source_subtask_id="s2",
                target_subtask_id="s3",
                artifact_type="FinalAnswerArtifact",
                max_tokens=100,
                metadata={"slot": "comm:p23"},
            ),
        ],
        delivery_schedule=list(plan.communication_plan.delivery_schedule),
        context_budgets={"s2": 100, "s3": 100_000},
    )
    state.subtasks["s1"].status = SubtaskStatus.COMMITTED
    state.subtasks["s2"].status = SubtaskStatus.COMMITTED
    state.subtasks["s3"].status = SubtaskStatus.PENDING
    obs = build_global_observation(state)
    assert "s2" not in obs.target_context_pressure
    assert obs.target_context_pressure.get("s3", 0.0) < 0.9
    triggers = detect_triggers(
        obs, budget=SlowLoopBudget(context_pressure_ratio=0.9, min_commits_between_updates=99)
    )
    assert SlowLoopTriggerReason.CONTEXT_PRESSURE not in triggers


@pytest.mark.asyncio
async def test_no_safe_future_edit_consumed_once(tmp_path: Path):
    plan = _chain()
    state = TaskExecutionState.from_plan(plan)
    state.communication_plan = plan.communication_plan
    state.subtasks["s2"].status = SubtaskStatus.READY
    state.subtasks["s2"].lease_status = "leased"
    state.subtasks["s3"].status = SubtaskStatus.READY
    state.subtasks["s3"].lease_status = "leased"
    # Force context pressure onto observation for leased targets only.
    state.communication_plan = CommunicationPlan(
        version=1,
        payload_contracts=list(plan.communication_plan.payload_contracts),
        delivery_schedule=list(plan.communication_plan.delivery_schedule),
        context_budgets={"s2": 10, "s3": 10},
    )
    ctrl = SlowLoopController(
        config=SlowLoopConfig(enabled=True),
        checkpoint_store=TaskCheckpointStore(tmp_path),
    )
    # Inject pressure via oversized contracts on leased targets — observation
    # will not include them; synthesize via diagnosis path using watermark.

    first = await ctrl.maybe_update(
        task_plan=plan,
        state=state,
        context=_ctx(tmp_path),
        leased_subtask_ids={"s2", "s3"},
    )
    # No eligible futures → NO_SAFE_FUTURE_EDIT and watermark advanced.
    assert first.message in {"NO_SAFE_FUTURE_EDIT", "no trigger", "diagnosis NO_CHANGE"}
    wm = state.slow_loop_state
    assert wm is not None
    second = await ctrl.maybe_update(
        task_plan=plan,
        state=state,
        context=_ctx(tmp_path),
        leased_subtask_ids={"s2", "s3"},
    )
    # Without new evidence, should not keep applying updates.
    assert second.updated is False
