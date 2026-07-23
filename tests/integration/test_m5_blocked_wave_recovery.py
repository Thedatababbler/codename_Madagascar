"""M5 blocked-wave recovery, history persistence, post-activation boundary."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from orchestra.communication.delivery import CommunicationDeliveryEngine
from orchestra.communication.ledger import DeliveryFailureReason
from orchestra.communication.payload import (
    DeliveryCondition,
    DeliveryRule,
    PayloadContract,
)
from orchestra.communication.plan import CommunicationPlan
from orchestra.control.input_assembler import SubtaskInputAssembler
from orchestra.control.ready_scheduler import (
    ReadySubtaskScheduler,
    SubtaskExecutionResult,
    SubtaskExecutionStatus,
)
from orchestra.control.slow_loop.controller import SlowLoopController
from orchestra.control.slow_loop.edits import apply_global_edits
from orchestra.control.slow_loop.revision import (
    RevisionAlreadyActivated,
    RevisionTransactionHooks,
    apply_projected_state_to_live,
    commit_prepared_revision,
    prepare_revision_staging,
)
from orchestra.control.slow_loop.schemas import (
    GlobalPlanRevision,
    GlobalPlanRevisionStatus,
    SlowLoopBudget,
    SlowLoopConfig,
    TaskSchedulingPolicy,
    UpsertDeliveryRuleEdit,
)
from orchestra.control.task_state import (
    SubtaskFailureReason,
    SubtaskStatus,
    TaskExecutionState,
)
from orchestra.decomposition.schemas import BudgetSpec, SubtaskSpec, TaskPlan
from orchestra.ir.artifacts import ArtifactBundle, create_artifact
from orchestra.runtime.backend import RunContext
from orchestra.runtime.limits import RuntimeLimits, RuntimeSemaphores
from orchestra.runtime.task_checkpoint import TaskCheckpointStore
from orchestra.schemas.artifacts import FinalAnswerArtifact
from orchestra.storage.artifacts import FileArtifactStore

GRAPH = "configs/graphs/codex_single_implementer.yaml"


def _ctx(tmp_path: Path, task_id: str = "m5bw") -> RunContext:
    limits = RuntimeLimits(
        max_parallel_benchmark_tasks=1,
        max_parallel_nodes_per_task=1,
        max_parallel_llm_calls=1,
        max_parallel_sandboxes=1,
    )
    return RunContext(
        run_id="m5bw",
        task_id=task_id,
        run_dir=tmp_path,
        limits=limits,
        semaphores=RuntimeSemaphores(limits),
        contract_hash="c",
    )


def _missing_rule_plan() -> TaskPlan:
    return TaskPlan(
        task_id="m5bw",
        plan_version=1,
        decomposition_rationale="blocked wave",
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
                objective="blocked then repaired",
                dependencies=["s1"],
                keystone_harness_id="repository_test_harness",
                local_graph_template=GRAPH,
                budget=BudgetSpec(),
            ),
        ],
        communication_plan=CommunicationPlan(
            version=1,
            payload_contracts=[
                PayloadContract(
                    payload_id="p1",
                    source_subtask_id="s1",
                    target_subtask_id="s2",
                    artifact_type="FinalAnswerArtifact",
                    required=True,
                )
            ],
            # Intentionally no DeliveryRule → REQUIRED_RULE_MISSING.
            delivery_schedule=[],
        ),
    )


async def _seed_state(tmp_path: Path) -> tuple[TaskPlan, TaskExecutionState, FileArtifactStore]:
    store = FileArtifactStore(tmp_path / "artifacts")
    plan = _missing_rule_plan()
    state = TaskExecutionState.from_plan(plan)
    state.communication_plan = plan.communication_plan
    state.scheduling_policy = TaskSchedulingPolicy(max_concurrent_subtasks=1)
    art = create_artifact(
        FinalAnswerArtifact(answer="upstream", source_node="s1"),
        producer_node_id="s1",
        task_id=plan.task_id,
    )
    await store.put(art)
    state.subtasks["s1"].status = SubtaskStatus.COMMITTED
    state.subtasks["s1"].final_output_artifact_id = art.artifact_id
    state.subtasks["s2"].status = SubtaskStatus.READY
    state.committed_subtask_count = 1
    return plan, state, store


def _scheduler(tmp_path: Path, store: FileArtifactStore) -> ReadySubtaskScheduler:
    ckpt = TaskCheckpointStore(tmp_path)
    # Runtime is unused when `_run_subtask_isolated` is stubbed.
    runtime = AsyncMock()
    runtime.artifact_store = store
    sched = ReadySubtaskScheduler(
        runtime=runtime,
        artifact_store=store,
        task_checkpoint_store=ckpt,
        contracts_dir="configs/contracts",
        slow_loop=SlowLoopController(
            config=SlowLoopConfig(
                enabled=True,
                budget=SlowLoopBudget(
                    min_commits_between_updates=0,
                    context_pressure_ratio=0.99,
                ),
            ),
            checkpoint_store=ckpt,
        ),
        slow_loop_config=SlowLoopConfig(enabled=True),
    )
    sched.input_assembler = SubtaskInputAssembler(store)
    return sched


@pytest.mark.asyncio
async def test_all_ready_blocked_invokes_slow_loop_repairs_and_reprereflights(tmp_path: Path):
    plan, state, store = await _seed_state(tmp_path)
    sched = _scheduler(tmp_path, store)

    deliverable = await sched._deliverable_ready_ids(
        state=state, task_plan=plan, initial_artifacts=ArtifactBundle()
    )
    assert deliverable == []
    assert state.subtasks["s2"].communication_block_reason == (
        DeliveryFailureReason.REQUIRED_RULE_MISSING.value
    )

    recovered = await sched._recover_blocked_wave(
        state=state,
        context=_ctx(tmp_path),
        ready_candidates=["s2"],
    )
    assert recovered is True
    assert state.active_plan_revision_id is not None
    assert any(
        isinstance(e, UpsertDeliveryRuleEdit) or getattr(e, "type", "") == "upsert_delivery_rule"
        for rev in state.plan_revision_history
        for e in getattr(rev, "edits", [])
    )
    assert state.subtasks["s2"].communication_block_reason is None

    deliverable2 = await sched._deliverable_ready_ids(
        state=state, task_plan=state.task_plan, initial_artifacts=ArtifactBundle()
    )
    assert deliverable2 == ["s2"]

    # Execution can continue once deliverable.
    local = state.subtasks["s2"].model_copy(deep=True)
    local.status = SubtaskStatus.AWAITING_CANONICAL_COMMIT
    art = create_artifact(
        FinalAnswerArtifact(answer="s2-out", source_node="s2"),
        producer_node_id="s2",
        task_id=plan.task_id,
    )
    await store.put(art)
    local.final_output_artifact_id = art.artifact_id
    local.candidate_artifacts = []

    async def _stub_run(**kwargs):  # noqa: ANN003
        sid = kwargs["subtask_id"]
        return SubtaskExecutionResult(
            subtask_id=sid,
            expected_state_version=kwargs.get("expected_state_version", 0),
            local_subtask_state=local,
            produced_artifacts=[art],
            execution_status=SubtaskExecutionStatus.SUCCESS_PENDING_COMMIT,
            candidate_harness_passed=True,
        )

    sched._run_subtask_isolated = _stub_run  # type: ignore[method-assign]
    state.subtasks["s2"].lease_status = "unleased"
    out = await sched.run_task(
        state.task_plan,
        state,
        initial_artifacts=ArtifactBundle(),
        context=_ctx(tmp_path),
    )
    assert out.subtasks["s2"].status is SubtaskStatus.COMMITTED


@pytest.mark.asyncio
async def test_blocked_wave_recovery_survives_checkpoint_restart(tmp_path: Path):
    plan, state, store = await _seed_state(tmp_path)
    sched = _scheduler(tmp_path, store)
    await sched._deliverable_ready_ids(
        state=state, task_plan=plan, initial_artifacts=ArtifactBundle()
    )
    assert state.subtasks["s2"].communication_block_reason
    # Persist blocked READY across restart; block reason must not remove eligibility.
    await TaskCheckpointStore(tmp_path).save(state)
    loaded = await TaskCheckpointStore(tmp_path).load("m5bw")
    assert loaded is not None
    assert loaded.subtasks["s2"].communication_block_reason
    assert "s2" in sched._ordered_ready_candidates(loaded, apply_limit=False)

    recovered = await sched._recover_blocked_wave(
        state=loaded,
        context=_ctx(tmp_path),
        ready_candidates=["s2"],
    )
    assert recovered is True
    deliverable = await sched._deliverable_ready_ids(
        state=loaded,
        task_plan=loaded.task_plan,
        initial_artifacts=ArtifactBundle(),
    )
    assert deliverable == ["s2"]


@pytest.mark.asyncio
async def test_applied_slow_loop_history_survives_restart_exactly_once(tmp_path: Path):
    plan, state, store = await _seed_state(tmp_path)
    ckpt = TaskCheckpointStore(tmp_path)
    ctrl = SlowLoopController(
        config=SlowLoopConfig(
            enabled=True,
            budget=SlowLoopBudget(min_commits_between_updates=0),
        ),
        checkpoint_store=ckpt,
    )
    state.subtasks["s2"].communication_block_reason = (
        DeliveryFailureReason.REQUIRED_RULE_MISSING.value
    )
    result = await ctrl.maybe_update(
        task_plan=plan,
        state=state,
        context=_ctx(tmp_path),
        leased_subtask_ids=set(),
    )
    assert result.updated is True
    rev_id = state.active_plan_revision_id
    assert rev_id
    assert len([r for r in state.plan_revision_history if r.revision_id == rev_id]) == 1
    assert len([r for r in state.slow_loop_history if r.record_id == rev_id]) == 1

    loaded = await ckpt.load("m5bw")
    assert loaded is not None
    assert loaded.active_plan_revision_id == rev_id
    assert len([r for r in loaded.plan_revision_history if r.revision_id == rev_id]) == 1
    assert len([r for r in loaded.slow_loop_history if r.record_id == rev_id]) == 1


@pytest.mark.asyncio
async def test_post_checkpoint_failure_cannot_claim_previous_plan(tmp_path: Path):
    plan, state, _store = await _seed_state(tmp_path)
    state.subtasks["s2"].status = SubtaskStatus.PENDING
    rule_edit = UpsertDeliveryRuleEdit(
        rule=DeliveryRule(rule_id="del_p1", payload_id="p1")
    )
    new_plan, new_comm, policy, _ = apply_global_edits(
        task_plan=plan,
        communication_plan=state.communication_plan,
        scheduling_policy=TaskSchedulingPolicy(),
        edits=[rule_edit],
        eligible_subtask_ids={"s2"},
    )
    rev = GlobalPlanRevision(
        revision_id="rev-post-act",
        revision_number=1,
        parent_plan_hash=plan.content_hash(),
        new_plan_hash=new_plan.content_hash(),
        parent_communication_hash="p",
        new_communication_hash="n",
        edits=[rule_edit],
        eligible_subtask_ids=["s2"],
        created_at_state_version=0,
    )
    prepared = prepare_revision_staging(
        run_dir=tmp_path,
        revision=rev,
        new_plan=new_plan,
        new_communication=new_comm,
        scheduling_policy=policy,
        state=state,
    )
    prepared.projected_state.slow_loop_history = list(state.slow_loop_history)

    def _boom() -> None:
        raise RuntimeError("crash after checkpoint activation")

    with pytest.raises(RevisionAlreadyActivated) as excinfo:
        await commit_prepared_revision(
            state=state,
            prepared=prepared,
            checkpoint_store=TaskCheckpointStore(tmp_path),
            run_dir=tmp_path,
            hooks=RevisionTransactionHooks(after_checkpoint_save=_boom),
        )
    # Live object may still be stale, but activation is recorded.
    assert state.active_plan_revision_id is None
    loaded = await TaskCheckpointStore(tmp_path).load("m5bw")
    assert loaded is not None
    assert loaded.active_plan_revision_id == "rev-post-act"

    # Controller-style recovery must mark updated, never keep_previous_plan.
    apply_projected_state_to_live(state, excinfo.value.projected_state)
    assert state.active_plan_revision_id == "rev-post-act"
    assert state.global_revision == 1
    # Post-activation errors must never be framed as keep_previous_plan.
    assert "keep_previous" not in str(excinfo.value).lower()
    assert isinstance(excinfo.value, RevisionAlreadyActivated)


@pytest.mark.asyncio
async def test_unrecoverable_blocked_wave_fail_closed(tmp_path: Path):
    plan, state, store = await _seed_state(tmp_path)
    # Unrepairable: required condition never — Slow Loop cannot safely edit.
    state.communication_plan = CommunicationPlan(
        version=1,
        payload_contracts=list(plan.communication_plan.payload_contracts),
        delivery_schedule=[
            DeliveryRule(
                rule_id="r1",
                payload_id="p1",
                condition=DeliveryCondition(kind="never"),
            )
        ],
    )
    plan = plan.model_copy(update={"communication_plan": state.communication_plan})
    state.task_plan = plan
    sched = _scheduler(tmp_path, store)
    await sched._deliverable_ready_ids(
        state=state, task_plan=plan, initial_artifacts=ArtifactBundle()
    )
    assert state.subtasks["s2"].communication_block_reason
    recovered = await sched._recover_blocked_wave(
        state=state,
        context=_ctx(tmp_path),
        ready_candidates=["s2"],
    )
    assert recovered is False
    assert state.subtasks["s2"].status is SubtaskStatus.FAILED
    assert state.subtasks["s2"].failure_reason is SubtaskFailureReason.INVALID_CONFIG
    assert "COMMUNICATION_BLOCKED_UNRECOVERABLE" in (
        state.subtasks["s2"].failure_message or ""
    )
    assert any(
        "COMMUNICATION_BLOCKED_UNRECOVERABLE" in (r.summary or "")
        for r in state.slow_loop_history
    )


@pytest.mark.asyncio
async def test_post_revision_delivery_succeeds_for_repaired_rule(tmp_path: Path):
    plan, state, store = await _seed_state(tmp_path)
    ctrl = SlowLoopController(
        config=SlowLoopConfig(
            enabled=True,
            budget=SlowLoopBudget(min_commits_between_updates=0),
        ),
        checkpoint_store=TaskCheckpointStore(tmp_path),
    )
    state.subtasks["s2"].communication_block_reason = (
        DeliveryFailureReason.REQUIRED_RULE_MISSING.value
    )
    result = await ctrl.maybe_update(
        task_plan=plan,
        state=state,
        context=_ctx(tmp_path),
        leased_subtask_ids=set(),
    )
    assert result.updated is True
    delivery = await CommunicationDeliveryEngine(store).deliver_for_target(
        task_plan=state.task_plan,
        task_state=state,
        communication_plan=state.communication_plan,
        target_subtask_id="s2",
    )
    assert delivery.blocked is False
    applied = [
        r
        for r in state.plan_revision_history
        if r.status is GlobalPlanRevisionStatus.APPLIED
    ]
    assert len(applied) == 1
