"""M5.4 integration: active-block consumption, attempt identity, usage resume."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from orchestra.backends.base import BackendSessionRef
from orchestra.communication.ledger import DeliveryFailureReason
from orchestra.communication.payload import DeliveryRule, PayloadContract
from orchestra.communication.plan import CommunicationPlan
from orchestra.control.backend_usage import BackendUsageRecord
from orchestra.control.slow_loop.candidate_generator import (
    RuleBasedGlobalCandidateGenerator,
)
from orchestra.control.slow_loop.controller import SlowLoopController
from orchestra.control.slow_loop.diagnosis import detect_triggers
from orchestra.control.slow_loop.observation import build_global_observation
from orchestra.control.slow_loop.schemas import (
    SlowLoopBudget,
    SlowLoopConfig,
    SlowLoopTriggerReason,
)
from orchestra.control.task_state import (
    BackendSessionRecord,
    SubtaskAttempt,
    SubtaskFailureReason,
    SubtaskStatus,
    TaskExecutionState,
)
from orchestra.decomposition.schemas import BudgetSpec, SubtaskSpec, TaskPlan
from orchestra.runtime.backend import RunContext
from orchestra.runtime.limits import RuntimeLimits, RuntimeSemaphores
from orchestra.runtime.task_checkpoint import TaskCheckpointStore

GRAPH = "configs/graphs/codex_single_implementer.yaml"


class _EmptyGen(RuleBasedGlobalCandidateGenerator):
    def generate(self, **kwargs):  # type: ignore[no-untyped-def]
        return []


def _ctx(tmp_path: Path) -> RunContext:
    limits = RuntimeLimits(
        max_parallel_benchmark_tasks=1,
        max_parallel_nodes_per_task=2,
        max_parallel_llm_calls=2,
        max_parallel_sandboxes=2,
    )
    return RunContext(
        run_id="m54",
        task_id="m54",
        run_dir=tmp_path,
        limits=limits,
        semaphores=RuntimeSemaphores(limits),
        contract_hash="c",
    )


def _chain() -> TaskPlan:
    return TaskPlan(
        task_id="m54",
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
        metadata={"task_budget": {"max_backend_calls": 100, "max_cost_usd": 10.0}},
    )


def _blocked_s3_state() -> tuple[TaskPlan, TaskExecutionState]:
    plan = _chain()
    state = TaskExecutionState.from_plan(plan)
    state.communication_plan = plan.communication_plan
    state.subtasks["s1"].status = SubtaskStatus.COMMITTED
    state.subtasks["s2"].status = SubtaskStatus.COMMITTED
    state.subtasks["s2"].final_output_artifact_id = "art-s2"
    state.subtasks["s3"].status = SubtaskStatus.READY
    state.subtasks["s3"].lease_status = "unleased"
    state.subtasks["s3"].communication_block_reason = (
        DeliveryFailureReason.REQUIRED_RULE_MISSING.value
    )
    return plan, state


@pytest.mark.asyncio
async def test_unresolved_block_consumed_once(tmp_path: Path):
    plan, state = _blocked_s3_state()
    ctrl = SlowLoopController(
        config=SlowLoopConfig(
            enabled=True, budget=SlowLoopBudget(min_commits_between_updates=99)
        ),
        generator=_EmptyGen(),
        checkpoint_store=TaskCheckpointStore(tmp_path),
    )
    first = await ctrl.maybe_update(
        task_plan=plan, state=state, context=_ctx(tmp_path), leased_subtask_ids=set()
    )
    assert SlowLoopTriggerReason.DELIVERY_FAILURE in first.trigger_reasons
    assert first.message == "NO_SAFE_FUTURE_EDIT"
    assert state.subtasks["s3"].communication_block_reason == (
        DeliveryFailureReason.REQUIRED_RULE_MISSING.value
    )
    second = await ctrl.maybe_update(
        task_plan=plan, state=state, context=_ctx(tmp_path), leased_subtask_ids=set()
    )
    assert SlowLoopTriggerReason.DELIVERY_FAILURE not in second.trigger_reasons
    assert second.updated is False
    assert second.revision is None
    assert state.slow_loop_state.updates_applied == 0


@pytest.mark.asyncio
async def test_changed_block_becomes_new_evidence(tmp_path: Path):
    plan, state = _blocked_s3_state()
    ctrl = SlowLoopController(
        config=SlowLoopConfig(
            enabled=True, budget=SlowLoopBudget(min_commits_between_updates=99)
        ),
        generator=_EmptyGen(),
        checkpoint_store=TaskCheckpointStore(tmp_path),
    )
    first = await ctrl.maybe_update(
        task_plan=plan, state=state, context=_ctx(tmp_path), leased_subtask_ids=set()
    )
    assert first.message == "NO_SAFE_FUTURE_EDIT"
    state.communication_plan = state.communication_plan.model_copy(update={"version": 2})
    # Keep the same block reason unresolved.
    assert state.subtasks["s3"].communication_block_reason is not None
    third = await ctrl.maybe_update(
        task_plan=plan, state=state, context=_ctx(tmp_path), leased_subtask_ids=set()
    )
    assert SlowLoopTriggerReason.DELIVERY_FAILURE in third.trigger_reasons
    assert third.message == "NO_SAFE_FUTURE_EDIT"


@pytest.mark.asyncio
async def test_two_backend_attempts_trigger_repeated_failure(tmp_path: Path):
    plan = _chain()
    state = TaskExecutionState.from_plan(plan)
    state.subtasks["s2"].status = SubtaskStatus.FAILED
    state.subtasks["s2"].failure_reason = SubtaskFailureReason.MODEL
    state.subtasks["s2"].backend_sessions = [
        BackendSessionRecord(
            node_id="codex_implementer",
            backend_id="codex_sdk",
            attempt_id=1,
            session_ref=BackendSessionRef(backend_id="codex_sdk", session_id="a"),
        ),
        BackendSessionRecord(
            node_id="codex_implementer",
            backend_id="codex_sdk",
            attempt_id=2,
            session_ref=BackendSessionRef(backend_id="codex_sdk", session_id="b"),
        ),
    ]
    state.subtasks["s3"].status = SubtaskStatus.PENDING
    obs = build_global_observation(state)
    triggers = detect_triggers(
        obs,
        budget=SlowLoopBudget(
            repeated_failure_threshold=2, min_commits_between_updates=99
        ),
    )
    assert SlowLoopTriggerReason.REPEATED_BACKEND_FAILURE in triggers
    ctrl = SlowLoopController(
        config=SlowLoopConfig(
            enabled=True,
            budget=SlowLoopBudget(
                repeated_failure_threshold=2, min_commits_between_updates=99
            ),
        ),
        generator=_EmptyGen(),
        checkpoint_store=TaskCheckpointStore(tmp_path),
    )
    result = await ctrl.maybe_update(
        task_plan=plan, state=state, context=_ctx(tmp_path), leased_subtask_ids=set()
    )
    assert SlowLoopTriggerReason.REPEATED_BACKEND_FAILURE in result.trigger_reasons


@pytest.mark.asyncio
async def test_two_harness_attempts_trigger_repeated_failure(tmp_path: Path):
    plan = _chain()
    state = TaskExecutionState.from_plan(plan)
    state.subtasks["s2"].status = SubtaskStatus.HARNESS_FAILED
    state.subtasks["s2"].failure_reason = SubtaskFailureReason.HARNESS
    state.subtasks["s2"].failure_message = "same harness error"
    state.subtasks["s2"].attempts = [
        SubtaskAttempt(attempt_id=1, status=SubtaskStatus.HARNESS_FAILED),
        SubtaskAttempt(attempt_id=2, status=SubtaskStatus.HARNESS_FAILED),
    ]
    obs = build_global_observation(state)
    triggers = detect_triggers(
        obs,
        budget=SlowLoopBudget(
            repeated_failure_threshold=2, min_commits_between_updates=99
        ),
    )
    assert SlowLoopTriggerReason.REPEATED_HARNESS_FAILURE in triggers
    ctrl = SlowLoopController(
        config=SlowLoopConfig(
            enabled=True,
            budget=SlowLoopBudget(
                repeated_failure_threshold=2, min_commits_between_updates=99
            ),
        ),
        generator=_EmptyGen(),
        checkpoint_store=TaskCheckpointStore(tmp_path),
    )
    result = await ctrl.maybe_update(
        task_plan=plan, state=state, context=_ctx(tmp_path), leased_subtask_ids=set()
    )
    assert SlowLoopTriggerReason.REPEATED_HARNESS_FAILURE in result.trigger_reasons


@pytest.mark.asyncio
async def test_checkpoint_resume_preserves_no_safe_watermark(tmp_path: Path):
    plan, state = _blocked_s3_state()
    store = TaskCheckpointStore(tmp_path)
    ctrl = SlowLoopController(
        config=SlowLoopConfig(
            enabled=True, budget=SlowLoopBudget(min_commits_between_updates=99)
        ),
        generator=_EmptyGen(),
        checkpoint_store=store,
    )
    first = await ctrl.maybe_update(
        task_plan=plan, state=state, context=_ctx(tmp_path), leased_subtask_ids=set()
    )
    assert first.message == "NO_SAFE_FUTURE_EDIT"
    loaded = await store.load(
        plan.task_id,
        plan_version=plan.plan_version,
        plan_content_hash=plan.content_hash(),
    )
    assert loaded is not None
    assert any(k.startswith("block:") for k in loaded.slow_loop_state.handled_evidence_keys)
    ctrl2 = SlowLoopController(
        config=SlowLoopConfig(
            enabled=True, budget=SlowLoopBudget(min_commits_between_updates=99)
        ),
        generator=_EmptyGen(),
        checkpoint_store=store,
    )
    second = await ctrl2.maybe_update(
        task_plan=plan, state=loaded, context=_ctx(tmp_path), leased_subtask_ids=set()
    )
    assert SlowLoopTriggerReason.DELIVERY_FAILURE not in second.trigger_reasons
    loaded.communication_plan = loaded.communication_plan.model_copy(
        update={"version": 3}
    )
    third = await ctrl2.maybe_update(
        task_plan=plan, state=loaded, context=_ctx(tmp_path), leased_subtask_ids=set()
    )
    assert SlowLoopTriggerReason.DELIVERY_FAILURE in third.trigger_reasons


@pytest.mark.asyncio
async def test_usage_accounting_survives_checkpoint_resume(tmp_path: Path):
    plan = _chain()
    state = TaskExecutionState.from_plan(plan)
    now = datetime.now(UTC)
    state.backend_usage_records = [
        BackendUsageRecord(
            usage_id="u-main",
            task_id="m54",
            subtask_id="s1",
            node_id="solver",
            backend_id="fake_backend",
            attempt_id=1,
            candidate_id=None,
            started_at=now,
            finished_at=now,
            latency_seconds=1.25,
            prompt_tokens=11,
            completion_tokens=7,
            estimated_cost_usd=0.002,
            accounting_source="ready_scheduler",
            status="success",
        ),
        BackendUsageRecord(
            usage_id="u-cand",
            task_id="m54",
            subtask_id="s1",
            node_id="solver",
            backend_id="fake_backend",
            attempt_id=2,
            candidate_id="cand-x",
            started_at=now,
            finished_at=now,
            latency_seconds=0.5,
            prompt_tokens=3,
            completion_tokens=4,
            estimated_cost_usd=0.001,
            accounting_source="fast_loop_candidate",
            status="harness_failed",
        ),
    ]
    store = TaskCheckpointStore(tmp_path)
    await store.save(state)
    loaded = await store.load(
        plan.task_id,
        plan_version=plan.plan_version,
        plan_content_hash=plan.content_hash(),
    )
    assert loaded is not None
    assert len(loaded.backend_usage_records) == 2
    by_id = {r.usage_id: r for r in loaded.backend_usage_records}
    assert by_id["u-main"].prompt_tokens == 11
    assert by_id["u-main"].latency_seconds == 1.25
    assert by_id["u-main"].attempt_id == 1
    assert by_id["u-cand"].candidate_id == "cand-x"
    assert by_id["u-cand"].status == "harness_failed"
    from orchestra.control.slow_loop.task_budget import TaskBudgetTracker

    snap = TaskBudgetTracker().snapshot(task_plan=plan, task_state=loaded)
    assert snap.objective_accounting.backend_calls == "exact"
    assert snap.objective_accounting.tokens == "exact"
    assert snap.objective_accounting.cost == "exact"
    assert snap.accounting_quality == "exact"
