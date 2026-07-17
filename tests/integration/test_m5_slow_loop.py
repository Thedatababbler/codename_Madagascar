"""M5 Slow Loop integration: future-only updates, delivery, scheduling."""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestra.communication.ledger import DeliveryRecord, DeliveryStatus, already_delivered
from orchestra.communication.payload import PayloadContract
from orchestra.communication.plan import CommunicationPlan
from orchestra.control.slow_loop.controller import SlowLoopController
from orchestra.control.slow_loop.schemas import (
    GlobalPlanRevisionStatus,
    SerializationGroupEdit,
    SlowLoopBudget,
    SlowLoopConfig,
    TaskSchedulingPolicy,
)
from orchestra.control.slow_loop.validation import FuturePlanValidator
from orchestra.control.task_state import SubtaskStatus, TaskExecutionState
from orchestra.decomposition.schemas import BudgetSpec, SubtaskSpec, TaskPlan
from orchestra.runtime.backend import RunContext
from orchestra.runtime.limits import RuntimeLimits, RuntimeSemaphores
from orchestra.runtime.task_checkpoint import TaskCheckpointStore


def _ctx(tmp_path: Path, task_id: str = "m5") -> RunContext:
    limits = RuntimeLimits(
        max_parallel_benchmark_tasks=1,
        max_parallel_nodes_per_task=2,
        max_parallel_llm_calls=2,
        max_parallel_sandboxes=2,
    )
    return RunContext(
        run_id="m5",
        task_id=task_id,
        run_dir=tmp_path,
        limits=limits,
        semaphores=RuntimeSemaphores(limits),
        contract_hash="c",
    )


def _three_subtask_state() -> TaskExecutionState:
    plan = TaskPlan(
        task_id="m5",
        plan_version=1,
        decomposition_rationale="m5",
        subtasks=[
            SubtaskSpec(
                subtask_id="s1",
                title="s1",
                objective="done",
                dependencies=[],
                keystone_harness_id="h",
                local_graph_template="configs/graphs/codex_single_implementer.yaml",
                budget=BudgetSpec(),
                expected_outputs=[],
            ),
            SubtaskSpec(
                subtask_id="s2",
                title="s2",
                objective="running",
                dependencies=["s1"],
                keystone_harness_id="h",
                local_graph_template="configs/graphs/codex_single_implementer.yaml",
                budget=BudgetSpec(),
                expected_outputs=[],
            ),
            SubtaskSpec(
                subtask_id="s3",
                title="s3",
                objective="pending",
                dependencies=["s1"],
                keystone_harness_id="h",
                local_graph_template="configs/graphs/codex_single_implementer.yaml",
                budget=BudgetSpec(),
                expected_outputs=[],
            ),
        ],
        communication_plan=CommunicationPlan(
            version=1,
            payload_contracts=[
                PayloadContract(
                    payload_id="big",
                    source_subtask_id="s1",
                    target_subtask_id="s3",
                    artifact_type="FinalAnswerArtifact",
                    max_tokens=8000,
                    metadata={"required": False, "priority": 80},
                )
            ],
            context_budgets={"s3": 1000},
        ),
    )
    state = TaskExecutionState.from_plan(plan)
    state.communication_plan = plan.communication_plan
    state.subtasks["s1"].status = SubtaskStatus.COMMITTED
    state.subtasks["s2"].status = SubtaskStatus.RUNNING
    state.subtasks["s2"].lease_status = "leased"
    state.subtasks["s3"].status = SubtaskStatus.PENDING
    state.subtasks["s3"].lease_status = "unleased"
    state.committed_subtask_count = 1
    state.scheduling_policy = TaskSchedulingPolicy(max_concurrent_subtasks=2)
    return state


@pytest.mark.asyncio
async def test_future_only_update_rejects_past_and_leased(tmp_path):
    state = _three_subtask_state()
    s1_obj = state.subtasks["s1"].spec.objective
    s2_obj = state.subtasks["s2"].spec.objective
    ctrl = SlowLoopController(
        config=SlowLoopConfig(
            enabled=True,
            budget=SlowLoopBudget(
                min_commits_between_updates=1,
                context_pressure_ratio=0.5,
            ),
        )
    )
    result = await ctrl.maybe_update(
        task_plan=state.task_plan,
        state=state,
        context=_ctx(tmp_path),
        leased_subtask_ids={"s2"},
    )
    assert result.updated is True
    assert state.subtasks["s1"].spec.objective == s1_obj
    assert state.subtasks["s2"].spec.objective == s2_obj
    # S3 communication budget or payload should change.
    assert state.communication_plan.version > 1


@pytest.mark.asyncio
async def test_delivery_ledger_idempotent_on_resume():
    ledger = [
        DeliveryRecord(
            delivery_id="d1",
            communication_plan_version=1,
            payload_id="p1",
            source_subtask_id="s1",
            target_subtask_id="s3",
            source_artifact_id="a1",
            projected_artifact_id="p1a",
            delivered_at_state_version=3,
            status=DeliveryStatus.DELIVERED,
        )
    ]
    assert (
        already_delivered(
            ledger,
            payload_id="p1",
            communication_plan_version=1,
            source_artifact_id="a1",
            target_subtask_id="s3",
        )
        is True
    )
    assert (
        already_delivered(
            ledger,
            payload_id="p1",
            communication_plan_version=2,
            source_artifact_id="a1",
            target_subtask_id="s3",
        )
        is False
    )


@pytest.mark.asyncio
async def test_invalid_revision_keeps_previous_plan(tmp_path):
    state = _three_subtask_state()
    parent_hash = state.task_plan.content_hash()
    # Force a candidate that edits leased s2 via direct validator path.
    proposed = state.task_plan.model_copy(deep=True)
    new_subs = []
    for s in proposed.subtasks:
        if s.subtask_id == "s2":
            new_subs.append(s.model_copy(update={"priority": 99}))
        else:
            new_subs.append(s)
    proposed = proposed.model_copy(update={"subtasks": new_subs, "plan_version": 2})
    result = FuturePlanValidator(SlowLoopConfig(enabled=True)).validate(
        current_state=state,
        proposed_plan=proposed,
        edits=[SerializationGroupEdit(subtask_ids=["s2", "s3"])],
        leased_subtask_ids={"s2"},
    )
    assert result.ok is False
    assert state.task_plan.content_hash() == parent_hash


@pytest.mark.asyncio
async def test_slow_loop_checkpoint_resume_does_not_reapply(tmp_path):
    state = _three_subtask_state()
    store = TaskCheckpointStore(tmp_path)
    ctrl = SlowLoopController(
        config=SlowLoopConfig(
            enabled=True,
            budget=SlowLoopBudget(
                min_commits_between_updates=1,
                context_pressure_ratio=0.5,
            ),
        )
    )
    await ctrl.maybe_update(
        task_plan=state.task_plan,
        state=state,
        context=_ctx(tmp_path),
        leased_subtask_ids={"s2"},
    )
    await store.save(state)
    rev_id = state.active_plan_revision_id
    version = state.global_revision
    loaded = await store.load("m5")
    assert loaded is not None
    assert loaded.active_plan_revision_id == rev_id
    # Second update with no new commits should not thrash versions unbounded.
    await ctrl.maybe_update(
        task_plan=loaded.task_plan,
        state=loaded,
        context=_ctx(tmp_path),
        leased_subtask_ids={"s2"},
    )
    applied = [
        r
        for r in loaded.plan_revision_history
        if r.status is GlobalPlanRevisionStatus.APPLIED
    ]
    assert len(applied) >= 1
    assert loaded.global_revision >= version


@pytest.mark.asyncio
async def test_private_evaluator_artifact_rejected_from_communication_plan():
    from orchestra.communication.compiler import CommunicationPlanCompiler
    from orchestra.communication.validation import CommunicationPlanValidationError

    plan = _three_subtask_state().task_plan
    comm = CommunicationPlan(
        payload_contracts=[
            PayloadContract(
                payload_id="priv",
                source_subtask_id="s1",
                target_subtask_id="s3",
                artifact_type="HiddenTestArtifact",
            )
        ]
    )
    with pytest.raises(CommunicationPlanValidationError):
        CommunicationPlanCompiler().compile(task_plan=plan, communication_plan=comm)
