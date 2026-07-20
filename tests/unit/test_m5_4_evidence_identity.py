"""M5.4 unit: typed evidence identity, active-block consumption, usage quality."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from orchestra.backends.base import BackendSessionRef
from orchestra.communication.ledger import DeliveryFailureReason
from orchestra.communication.payload import DeliveryRule, PayloadContract
from orchestra.communication.plan import CommunicationPlan
from orchestra.control.backend_usage import (
    BackendUsageRecord,
    ObjectiveAccountingQuality,
)
from orchestra.control.slow_loop.controller import SlowLoopController
from orchestra.control.slow_loop.diagnosis import detect_triggers
from orchestra.control.slow_loop.evidence import (
    active_block_evidence_key,
    backend_evidence_key,
    collect_backend_failure_events,
    collect_harness_failure_events,
    harness_evidence_key,
)
from orchestra.control.slow_loop.observation import (
    advance_observation_watermark,
    build_global_observation,
)
from orchestra.control.slow_loop.schemas import (
    SlowLoopBudget,
    SlowLoopConfig,
    SlowLoopTriggerReason,
)
from orchestra.control.slow_loop.task_budget import TaskBudgetTracker
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


def _plan() -> TaskPlan:
    return TaskPlan(
        task_id="m54",
        plan_version=1,
        decomposition_rationale="m54",
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


def _session(attempt_id: int, session_id: str, *, candidate_id: str | None = None):
    return BackendSessionRecord(
        node_id="codex_implementer",
        backend_id="codex_sdk",
        attempt_id=attempt_id,
        session_ref=BackendSessionRef(backend_id="codex_sdk", session_id=session_id),
        candidate_id=candidate_id,
    )


def test_backend_evidence_uses_real_attempt_id():
    plan = _plan()
    state = TaskExecutionState.from_plan(plan)
    state.subtasks["s2"].failure_reason = SubtaskFailureReason.MODEL
    state.subtasks["s2"].backend_sessions = [_session(3, "sess-8ac2", candidate_id="cand-b")]
    events = collect_backend_failure_events(state)
    assert len(events) == 1
    assert events[0].attempt_id == 3
    assert ":3:" in events[0].evidence_key
    assert "cand-b" in events[0].evidence_key
    assert "sess-8ac2" in events[0].evidence_key
    assert "1" != events[0].evidence_key.split(":")[3]


def test_backend_same_backend_two_attempts_produce_two_events():
    plan = _plan()
    state = TaskExecutionState.from_plan(plan)
    state.subtasks["s2"].failure_reason = SubtaskFailureReason.MODEL
    state.subtasks["s2"].backend_sessions = [
        _session(1, "sess-a"),
        _session(2, "sess-b"),
    ]
    events = collect_backend_failure_events(state)
    keys = {e.evidence_key for e in events}
    assert len(keys) == 2


def test_repeated_backend_failure_threshold_reached():
    plan = _plan()
    state = TaskExecutionState.from_plan(plan)
    state.subtasks["s2"].failure_reason = SubtaskFailureReason.MODEL
    state.subtasks["s2"].backend_sessions = [
        _session(1, "sess-a"),
        _session(2, "sess-b"),
    ]
    obs = build_global_observation(state)
    assert obs.recent_backend_failure_counts.get("codex_sdk", 0) >= 2
    triggers = detect_triggers(
        obs,
        budget=SlowLoopBudget(
            repeated_failure_threshold=2, min_commits_between_updates=99
        ),
    )
    assert SlowLoopTriggerReason.REPEATED_BACKEND_FAILURE in triggers


def test_harness_evidence_uses_subtask_attempt_id():
    plan = _plan()
    state = TaskExecutionState.from_plan(plan)
    state.subtasks["s2"].status = SubtaskStatus.HARNESS_FAILED
    state.subtasks["s2"].failure_reason = SubtaskFailureReason.HARNESS
    state.subtasks["s2"].failure_message = "same error"
    state.subtasks["s2"].attempts = [
        SubtaskAttempt(
            attempt_id=7,
            status=SubtaskStatus.HARNESS_FAILED,
            metadata={"harness_artifact_id": "h-7"},
        )
    ]
    events = collect_harness_failure_events(state)
    assert any(e.attempt_id == 7 for e in events)
    key = harness_evidence_key(
        subtask_id="s2",
        attempt_id=7,
        harness_artifact_id="h-7",
        failure_reason=SubtaskFailureReason.HARNESS.value,
    )
    assert key in {e.evidence_key for e in events}


def test_same_harness_message_different_attempts_are_distinct():
    plan = _plan()
    state = TaskExecutionState.from_plan(plan)
    state.subtasks["s2"].status = SubtaskStatus.HARNESS_FAILED
    state.subtasks["s2"].failure_reason = SubtaskFailureReason.HARNESS
    state.subtasks["s2"].failure_message = "identical text"
    state.subtasks["s2"].attempts = [
        SubtaskAttempt(attempt_id=1, status=SubtaskStatus.HARNESS_FAILED),
        SubtaskAttempt(attempt_id=2, status=SubtaskStatus.HARNESS_FAILED),
    ]
    events = collect_harness_failure_events(state)
    assert len({e.evidence_key for e in events}) >= 2


def test_repeated_harness_failure_threshold_reached():
    plan = _plan()
    state = TaskExecutionState.from_plan(plan)
    state.subtasks["s2"].status = SubtaskStatus.HARNESS_FAILED
    state.subtasks["s2"].failure_reason = SubtaskFailureReason.HARNESS
    state.subtasks["s2"].attempts = [
        SubtaskAttempt(attempt_id=1, status=SubtaskStatus.HARNESS_FAILED),
        SubtaskAttempt(attempt_id=2, status=SubtaskStatus.HARNESS_FAILED),
    ]
    obs = build_global_observation(state)
    assert obs.recent_harness_failures >= 2
    triggers = detect_triggers(
        obs,
        budget=SlowLoopBudget(
            repeated_failure_threshold=2, min_commits_between_updates=99
        ),
    )
    assert SlowLoopTriggerReason.REPEATED_HARNESS_FAILURE in triggers


def test_active_block_first_observation_triggers():
    plan = _plan()
    state = TaskExecutionState.from_plan(plan)
    state.subtasks["s3"].status = SubtaskStatus.READY
    state.subtasks["s3"].communication_block_reason = (
        DeliveryFailureReason.REQUIRED_RULE_MISSING.value
    )
    obs = build_global_observation(state)
    assert "s3" in obs.recent_active_block_reasons
    assert any(k.startswith("block:") for k in obs.new_evidence_keys)
    triggers = detect_triggers(
        obs, budget=SlowLoopBudget(min_commits_between_updates=99)
    )
    assert SlowLoopTriggerReason.DELIVERY_FAILURE in triggers


def test_handled_active_block_does_not_retrigger():
    plan = _plan()
    state = TaskExecutionState.from_plan(plan)
    state.subtasks["s3"].status = SubtaskStatus.READY
    state.subtasks["s3"].communication_block_reason = (
        DeliveryFailureReason.REQUIRED_RULE_MISSING.value
    )
    obs1 = build_global_observation(state)
    advance_observation_watermark(state, observation=obs1)
    assert state.subtasks["s3"].communication_block_reason is not None
    obs2 = build_global_observation(state)
    assert obs2.recent_active_block_reasons == {}
    triggers = detect_triggers(
        obs2, budget=SlowLoopBudget(min_commits_between_updates=99)
    )
    assert SlowLoopTriggerReason.DELIVERY_FAILURE not in triggers


def test_active_block_reason_change_retriggers():
    plan = _plan()
    state = TaskExecutionState.from_plan(plan)
    state.subtasks["s3"].status = SubtaskStatus.READY
    state.subtasks["s3"].communication_block_reason = (
        DeliveryFailureReason.REQUIRED_RULE_MISSING.value
    )
    obs1 = build_global_observation(state)
    advance_observation_watermark(state, observation=obs1)
    state.subtasks["s3"].communication_block_reason = (
        DeliveryFailureReason.REQUIRED_ARTIFACT_MISSING.value
    )
    obs2 = build_global_observation(state)
    assert SlowLoopTriggerReason.DELIVERY_FAILURE in detect_triggers(
        obs2, budget=SlowLoopBudget(min_commits_between_updates=99)
    )


def test_active_block_plan_version_change_retriggers():
    plan = _plan()
    state = TaskExecutionState.from_plan(plan)
    state.subtasks["s3"].status = SubtaskStatus.READY
    state.subtasks["s3"].communication_block_reason = (
        DeliveryFailureReason.REQUIRED_RULE_MISSING.value
    )
    obs1 = build_global_observation(state)
    advance_observation_watermark(state, observation=obs1)
    state.communication_plan = state.communication_plan.model_copy(update={"version": 2})
    obs2 = build_global_observation(state)
    assert SlowLoopTriggerReason.DELIVERY_FAILURE in detect_triggers(
        obs2, budget=SlowLoopBudget(min_commits_between_updates=99)
    )


def test_active_block_source_artifact_change_retriggers():
    plan = _plan()
    state = TaskExecutionState.from_plan(plan)
    state.subtasks["s2"].status = SubtaskStatus.COMMITTED
    state.subtasks["s2"].final_output_artifact_id = "art-old"
    state.subtasks["s3"].status = SubtaskStatus.READY
    state.subtasks["s3"].communication_block_reason = (
        DeliveryFailureReason.REQUIRED_RULE_MISSING.value
    )
    key1 = active_block_evidence_key(
        state=state,
        target_subtask_id="s3",
        reason=DeliveryFailureReason.REQUIRED_RULE_MISSING.value,
    )
    obs1 = build_global_observation(state)
    advance_observation_watermark(state, observation=obs1)
    state.subtasks["s2"].final_output_artifact_id = "art-new"
    key2 = active_block_evidence_key(
        state=state,
        target_subtask_id="s3",
        reason=DeliveryFailureReason.REQUIRED_RULE_MISSING.value,
    )
    assert key1 != key2
    obs2 = build_global_observation(state)
    assert SlowLoopTriggerReason.DELIVERY_FAILURE in detect_triggers(
        obs2, budget=SlowLoopBudget(min_commits_between_updates=99)
    )


@pytest.mark.asyncio
async def test_no_safe_watermark_contains_active_block_key(tmp_path: Path):
    from orchestra.control.slow_loop.candidate_generator import (
        RuleBasedGlobalCandidateGenerator,
    )

    class _Empty(RuleBasedGlobalCandidateGenerator):
        def generate(self, **kwargs):  # type: ignore[no-untyped-def]
            return []

    plan = _plan()
    state = TaskExecutionState.from_plan(plan)
    state.subtasks["s3"].status = SubtaskStatus.READY
    state.subtasks["s3"].communication_block_reason = (
        DeliveryFailureReason.REQUIRED_RULE_MISSING.value
    )
    limits = RuntimeLimits(
        max_parallel_benchmark_tasks=1,
        max_parallel_nodes_per_task=1,
        max_parallel_llm_calls=1,
        max_parallel_sandboxes=1,
    )
    ctx = RunContext(
        run_id="m54",
        task_id="m54",
        run_dir=tmp_path,
        limits=limits,
        semaphores=RuntimeSemaphores(limits),
        contract_hash="c",
    )
    ctrl = SlowLoopController(
        config=SlowLoopConfig(
            enabled=True, budget=SlowLoopBudget(min_commits_between_updates=99)
        ),
        generator=_Empty(),
        checkpoint_store=TaskCheckpointStore(tmp_path),
    )
    result = await ctrl.maybe_update(
        task_plan=plan, state=state, context=ctx, leased_subtask_ids=set()
    )
    assert result.message == "NO_SAFE_FUTURE_EDIT"
    assert any(k.startswith("block:") for k in state.slow_loop_state.handled_evidence_keys)


@pytest.mark.asyncio
async def test_controller_failure_does_not_consume_evidence(tmp_path: Path):
    from orchestra.control.slow_loop.candidate_generator import (
        RuleBasedGlobalCandidateGenerator,
    )

    class _Empty(RuleBasedGlobalCandidateGenerator):
        def generate(self, **kwargs):  # type: ignore[no-untyped-def]
            return []

    plan = _plan()
    state = TaskExecutionState.from_plan(plan)
    state.subtasks["s3"].status = SubtaskStatus.READY
    state.subtasks["s3"].communication_block_reason = (
        DeliveryFailureReason.REQUIRED_RULE_MISSING.value
    )
    limits = RuntimeLimits(
        max_parallel_benchmark_tasks=1,
        max_parallel_nodes_per_task=1,
        max_parallel_llm_calls=1,
        max_parallel_sandboxes=1,
    )
    ctx = RunContext(
        run_id="m54",
        task_id="m54",
        run_dir=tmp_path,
        limits=limits,
        semaphores=RuntimeSemaphores(limits),
        contract_hash="c",
    )
    store = TaskCheckpointStore(tmp_path)
    store.save = AsyncMock(side_effect=RuntimeError("checkpoint boom"))
    ctrl = SlowLoopController(
        config=SlowLoopConfig(
            enabled=True, budget=SlowLoopBudget(min_commits_between_updates=99)
        ),
        generator=_Empty(),
        checkpoint_store=store,
    )
    with pytest.raises(RuntimeError, match="checkpoint boom"):
        await ctrl.maybe_update(
            task_plan=plan, state=state, context=ctx, leased_subtask_ids=set()
        )
    handled = set(getattr(state.slow_loop_state, "handled_evidence_keys", []) or [])
    assert not any(k.startswith("block:") for k in handled)
    obs = build_global_observation(state)
    assert SlowLoopTriggerReason.DELIVERY_FAILURE in detect_triggers(
        obs, budget=SlowLoopBudget(min_commits_between_updates=99)
    )


def test_partial_usage_does_not_report_exact_cost():
    plan = _plan()
    state = TaskExecutionState.from_plan(plan)
    now = datetime.now(UTC)
    state.backend_usage_records = [
        BackendUsageRecord(
            usage_id="u1",
            task_id="m54",
            subtask_id="s1",
            node_id="n",
            backend_id="fake",
            attempt_id=1,
            started_at=now,
            finished_at=now,
            latency_seconds=0.1,
            prompt_tokens=10,
            completion_tokens=5,
            estimated_cost_usd=None,
            accounting_source="test",
            status="ok",
        )
    ]
    plan = plan.model_copy(
        update={"metadata": {"task_budget": {"max_backend_calls": 10, "max_cost_usd": 1.0}}}
    )
    snap = TaskBudgetTracker().snapshot(task_plan=plan, task_state=state)
    assert snap.objective_accounting.backend_calls == "exact"
    assert snap.objective_accounting.cost == "unavailable"
    assert snap.accounting_quality != "exact"


def test_complete_usage_reports_exact_dimensions():
    plan = _plan()
    state = TaskExecutionState.from_plan(plan)
    now = datetime.now(UTC)
    state.backend_usage_records = [
        BackendUsageRecord(
            usage_id="u1",
            task_id="m54",
            subtask_id="s1",
            node_id="n",
            backend_id="fake",
            attempt_id=1,
            started_at=now,
            finished_at=now,
            latency_seconds=0.2,
            prompt_tokens=10,
            completion_tokens=5,
            estimated_cost_usd=0.001,
            accounting_source="test",
            status="ok",
        )
    ]
    plan = plan.model_copy(
        update={"metadata": {"task_budget": {"max_backend_calls": 10, "max_cost_usd": 1.0}}}
    )
    snap = TaskBudgetTracker().snapshot(task_plan=plan, task_state=state)
    assert snap.objective_accounting == ObjectiveAccountingQuality(
        backend_calls="exact",
        tokens="exact",
        cost="exact",
        latency="exact",
    )
    assert snap.accounting_quality == "exact"


def test_usage_records_include_fast_loop_candidates():
    plan = _plan()
    state = TaskExecutionState.from_plan(plan)
    now = datetime.now(UTC)
    state.backend_usage_records = [
        BackendUsageRecord(
            usage_id="main",
            task_id="m54",
            subtask_id="s2",
            node_id="n",
            backend_id="fake",
            attempt_id=1,
            candidate_id=None,
            started_at=now,
            finished_at=now,
            latency_seconds=0.1,
            prompt_tokens=1,
            completion_tokens=1,
            estimated_cost_usd=0.0,
            accounting_source="ready_scheduler",
            status="ok",
        ),
        BackendUsageRecord(
            usage_id="cand",
            task_id="m54",
            subtask_id="s2",
            node_id="n",
            backend_id="fake",
            attempt_id=2,
            candidate_id="cand-1",
            started_at=now,
            finished_at=now,
            latency_seconds=0.2,
            prompt_tokens=2,
            completion_tokens=2,
            estimated_cost_usd=0.0,
            accounting_source="fast_loop_candidate",
            status="ok",
        ),
    ]
    assert any(r.candidate_id == "cand-1" for r in state.backend_usage_records)
    assert any(r.accounting_source == "fast_loop_candidate" for r in state.backend_usage_records)


def test_backend_evidence_key_format():
    sess = _session(3, "sess-8ac2", candidate_id="cand-b")
    key = backend_evidence_key(
        subtask_id="s2", session=sess, failure_reason="model"
    )
    assert key == "backend:s2:codex_implementer:3:cand-b:codex_sdk:model:sess-8ac2"


def test_session_existence_alone_is_not_exact_cost():
    plan = _plan()
    state = TaskExecutionState.from_plan(plan)
    state.subtasks["s1"].backend_sessions = [_session(1, "sess-x")]
    plan = plan.model_copy(
        update={"metadata": {"task_budget": {"max_backend_calls": 10, "max_cost_usd": 1.0}}}
    )
    snap = TaskBudgetTracker().snapshot(task_plan=plan, task_state=state)
    assert snap.accounting_quality != "exact"
    assert snap.objective_accounting.cost != "exact"
