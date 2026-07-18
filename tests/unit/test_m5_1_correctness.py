"""M5.1 correctness: final graph paths, crash order, aggregation budget, preflight."""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestra.communication.aggregation import (
    AggregationInput,
    AggregationRule,
    AggregationStrategy,
    aggregate_payloads,
)
from orchestra.communication.budget import FinalDeliveryUnit, pack_final_delivery_units
from orchestra.communication.delivery import CommunicationDeliveryEngine
from orchestra.communication.ledger import DeliveryFailureReason, DeliveryStatus
from orchestra.communication.payload import (
    DeliveryCondition,
    DeliveryRule,
    DeliveryTrigger,
    PayloadContract,
)
from orchestra.communication.plan import CommunicationPlan
from orchestra.communication.projection import estimate_tokens
from orchestra.control.slow_loop.edits import apply_global_edits
from orchestra.control.slow_loop.graph_materializer import FutureGraphMaterializer
from orchestra.control.slow_loop.revision import (
    PlanRevisionIdCollision,
    RevisionTransactionHooks,
    commit_prepared_revision,
    prepare_revision_staging,
    verify_checkpoint_revision_consistency,
)
from orchestra.control.slow_loop.schemas import (
    ContextBudgetEdit,
    GlobalPlanRevision,
    GlobalPlanRevisionStatus,
    PendingBackendAssignmentEdit,
    TaskSchedulingPolicy,
)
from orchestra.control.task_state import SubtaskStatus, TaskExecutionState
from orchestra.decomposition.schemas import BudgetSpec, SubtaskSpec, TaskPlan
from orchestra.ir.artifacts import create_artifact
from orchestra.runtime.task_checkpoint import TaskCheckpointStore
from orchestra.schemas.artifacts import FinalAnswerArtifact
from orchestra.storage.artifacts import FileArtifactStore

GRAPH = "configs/graphs/codex_single_implementer.yaml"


def _plan() -> TaskPlan:
    return TaskPlan(
        task_id="t51",
        plan_version=1,
        decomposition_rationale="x",
        subtasks=[
            SubtaskSpec(
                subtask_id="s1",
                title="s1",
                objective="s1",
                dependencies=[],
                keystone_harness_id="repository_test_harness",
                local_graph_template=GRAPH,
                budget=BudgetSpec(),
            ),
            SubtaskSpec(
                subtask_id="s2",
                title="s2",
                objective="s2",
                dependencies=["s1"],
                keystone_harness_id="repository_test_harness",
                local_graph_template=GRAPH,
                budget=BudgetSpec(),
            ),
        ],
    )


def test_materialized_plan_stores_final_revision_path(tmp_path: Path):
    plan = _plan()
    state = TaskExecutionState.from_plan(plan)
    state.subtasks["s2"].status = SubtaskStatus.PENDING
    new_plan, new_comm, policy, _ = apply_global_edits(
        task_plan=plan,
        communication_plan=state.communication_plan,
        scheduling_policy=TaskSchedulingPolicy(),
        edits=[
            PendingBackendAssignmentEdit(
                subtask_id="s2",
                node_id="codex_implementer",
                backend_id="smolagents_code",
                model_name="gpt-4o-mini",
            )
        ],
        eligible_subtask_ids={"s2"},
    )
    rev = GlobalPlanRevision(
        revision_id="rev-path-1",
        revision_number=1,
        parent_plan_hash=plan.content_hash(),
        new_plan_hash=new_plan.content_hash(),
        parent_communication_hash="p",
        new_communication_hash="n",
        edits=list(
            [
                PendingBackendAssignmentEdit(
                    subtask_id="s2",
                    node_id="codex_implementer",
                    backend_id="smolagents_code",
                    model_name="gpt-4o-mini",
                )
            ]
        ),
        eligible_subtask_ids=["s2"],
        created_at_state_version=0,
        status=GlobalPlanRevisionStatus.VALIDATED,
    )
    prepared = prepare_revision_staging(
        run_dir=tmp_path,
        revision=rev,
        new_plan=new_plan,
        new_communication=new_comm,
        scheduling_policy=policy,
        state=state,
        allowed_backend_pools={"default": ["smolagents_code", "codex_sdk"]},
        backend_model_pools={"smolagents_code": ["gpt-4o-mini"]},
    )
    s2 = next(s for s in prepared.proposed_task_plan.subtasks if s.subtask_id == "s2")
    path = str(s2.metadata.get("materialized_graph_path") or s2.local_graph_template)
    assert ".staging-" not in path
    assert path.endswith("plan_revisions/rev-path-1/graphs/s2.yaml")


def test_materialized_graph_path_never_contains_staging(tmp_path: Path):
    mat = FutureGraphMaterializer().materialize(
        subtask=SubtaskSpec(
            subtask_id="s3",
            title="s3",
            objective="o",
            dependencies=[],
            keystone_harness_id="repository_test_harness",
            local_graph_template=GRAPH,
            budget=BudgetSpec(),
            metadata={
                "backend_assignment": {
                    "node_id": "codex_implementer",
                    "backend_id": "structured_llm",
                }
            },
        ),
        staging_graphs_dir=tmp_path / ".staging-rev-x" / "graphs",
        final_graphs_dir=tmp_path / "rev-x" / "graphs",
        revision_id="rev-x",
        allowed_backend_pools={"default": ["structured_llm", "codex_sdk"]},
    )
    assert ".staging-" not in mat.graph_path
    assert mat.paths is not None
    assert ".staging-" in mat.paths.staging_path
    assert mat.graph_path == mat.paths.final_path


@pytest.mark.asyncio
async def test_revision_promote_precedes_checkpoint_activation(tmp_path: Path):
    plan = _plan()
    state = TaskExecutionState.from_plan(plan)
    state.subtasks["s2"].status = SubtaskStatus.PENDING
    new_plan, new_comm, policy, _ = apply_global_edits(
        task_plan=plan,
        communication_plan=state.communication_plan,
        scheduling_policy=TaskSchedulingPolicy(),
        edits=[ContextBudgetEdit(target_subtask_id="s2", max_tokens=55)],
        eligible_subtask_ids={"s2"},
    )
    rev = GlobalPlanRevision(
        revision_id="rev-order-1",
        revision_number=1,
        parent_plan_hash=plan.content_hash(),
        new_plan_hash=new_plan.content_hash(),
        parent_communication_hash="p",
        new_communication_hash="n",
        edits=[ContextBudgetEdit(target_subtask_id="s2", max_tokens=55)],
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
    seen: list[str] = []

    class TrackingStore(TaskCheckpointStore):
        async def save(self, st):  # noqa: ANN001
            seen.append("checkpoint")
            # Revision must already be promoted before checkpoint activation.
            assert (tmp_path / "plan_revisions" / "rev-order-1").exists()
            await super().save(st)

    await commit_prepared_revision(
        state=state,
        prepared=prepared,
        checkpoint_store=TrackingStore(tmp_path),
        run_dir=tmp_path,
        hooks=RevisionTransactionHooks(
            after_revision_promote=lambda: seen.append("promote"),
        ),
    )
    assert seen.index("promote") < seen.index("checkpoint")
    assert state.active_plan_revision_id == "rev-order-1"


@pytest.mark.asyncio
async def test_orphan_revision_is_not_active(tmp_path: Path):
    plan = _plan()
    state = TaskExecutionState.from_plan(plan)
    state.subtasks["s2"].status = SubtaskStatus.PENDING
    new_plan, new_comm, policy, _ = apply_global_edits(
        task_plan=plan,
        communication_plan=state.communication_plan,
        scheduling_policy=TaskSchedulingPolicy(),
        edits=[ContextBudgetEdit(target_subtask_id="s2", max_tokens=66)],
        eligible_subtask_ids={"s2"},
    )
    rev = GlobalPlanRevision(
        revision_id="rev-orphan-1",
        revision_number=1,
        parent_plan_hash=plan.content_hash(),
        new_plan_hash=new_plan.content_hash(),
        parent_communication_hash="p",
        new_communication_hash="n",
        edits=[ContextBudgetEdit(target_subtask_id="s2", max_tokens=66)],
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

    class Boom(TaskCheckpointStore):
        async def save(self, st):  # noqa: ANN001
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await commit_prepared_revision(
            state=state,
            prepared=prepared,
            checkpoint_store=Boom(tmp_path),
            run_dir=tmp_path,
        )
    assert state.active_plan_revision_id is None
    assert (tmp_path / "plan_revisions" / "rev-orphan-1").exists()


@pytest.mark.asyncio
async def test_revision_id_collision_fails_closed(tmp_path: Path):
    plan = _plan()
    state = TaskExecutionState.from_plan(plan)
    state.subtasks["s2"].status = SubtaskStatus.PENDING
    new_plan, new_comm, policy, _ = apply_global_edits(
        task_plan=plan,
        communication_plan=state.communication_plan,
        scheduling_policy=TaskSchedulingPolicy(),
        edits=[ContextBudgetEdit(target_subtask_id="s2", max_tokens=11)],
        eligible_subtask_ids={"s2"},
    )
    rev = GlobalPlanRevision(
        revision_id="rev-col-1",
        revision_number=1,
        parent_plan_hash=plan.content_hash(),
        new_plan_hash=new_plan.content_hash(),
        parent_communication_hash="p",
        new_communication_hash="n",
        edits=[ContextBudgetEdit(target_subtask_id="s2", max_tokens=11)],
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
    await commit_prepared_revision(
        state=state,
        prepared=prepared,
        checkpoint_store=TaskCheckpointStore(tmp_path),
        run_dir=tmp_path,
    )
    # Second prepare with same id but different hash content.
    other_plan, other_comm, other_policy, _ = apply_global_edits(
        task_plan=plan,
        communication_plan=state.communication_plan,
        scheduling_policy=TaskSchedulingPolicy(),
        edits=[ContextBudgetEdit(target_subtask_id="s2", max_tokens=999)],
        eligible_subtask_ids={"s2"},
    )
    rev2 = rev.model_copy(
        update={
            "new_plan_hash": other_plan.content_hash(),
            "edits": [ContextBudgetEdit(target_subtask_id="s2", max_tokens=999)],
        }
    )
    prepared2 = prepare_revision_staging(
        run_dir=tmp_path,
        revision=rev2,
        new_plan=other_plan,
        new_communication=other_comm,
        scheduling_policy=other_policy,
        state=TaskExecutionState.from_plan(plan),
    )
    with pytest.raises(PlanRevisionIdCollision):
        await commit_prepared_revision(
            state=TaskExecutionState.from_plan(plan),
            prepared=prepared2,
            checkpoint_store=TaskCheckpointStore(tmp_path / "other"),
            run_dir=tmp_path,
        )


def test_final_delivery_unit_budget_uses_aggregate_tokens():
    a1 = create_artifact(
        FinalAnswerArtifact(answer='{"fields":{"v":1}}', source_node="s1"),
        producer_node_id="s1",
        task_id="t",
    )
    a2 = create_artifact(
        FinalAnswerArtifact(answer='{"fields":{"v":2}}', source_node="s2"),
        producer_node_id="s2",
        task_id="t",
    )
    agg = aggregate_payloads(
        rule=AggregationRule(
            rule_id="agg1",
            source_payload_ids=["p1", "p2"],
            strategy=AggregationStrategy.CONCAT_TEXT,
            delimiter="\n",
            max_tokens=10_000,
            target_slot="shared",
        ),
        inputs=AggregationInput(
            source_payload_ids=["p1", "p2"],
            source_artifact_ids=[a1.artifact_id, a2.artifact_id],
            projected_artifacts=[a1, a2],
        ),
        task_id="t",
    )
    assert agg.estimated_tokens == estimate_tokens(agg.aggregated_artifact.payload)
    unit = FinalDeliveryUnit(
        delivery_unit_id="u1",
        target_subtask_id="s3",
        target_slot="shared",
        source_payload_ids=["p1", "p2"],
        source_artifact_ids=[a1.artifact_id, a2.artifact_id],
        artifact=agg.aggregated_artifact,
        estimated_tokens=agg.estimated_tokens,
        required=False,
        aggregation_rule_id="agg1",
    )
    result = pack_final_delivery_units(
        target_subtask_id="s3",
        max_tokens=max(1, agg.estimated_tokens - 1),
        units=[unit],
        fail_closed_on_required=False,
    )
    assert unit.delivery_unit_id in result.omitted_unit_ids
    assert unit.delivery_unit_id not in result.included_unit_ids


def test_aggregate_excluded_when_context_budget_rejects_it():
    art = create_artifact(
        FinalAnswerArtifact(answer="x" * 2000, source_node="agg"),
        producer_node_id="agg",
        task_id="t",
    )
    unit = FinalDeliveryUnit(
        delivery_unit_id="u-agg",
        target_subtask_id="s3",
        target_slot="shared",
        artifact=art,
        estimated_tokens=estimate_tokens(art.payload),
        required=False,
        aggregation_rule_id="agg1",
    )
    result = pack_final_delivery_units(
        target_subtask_id="s3",
        max_tokens=10,
        units=[unit],
        fail_closed_on_required=False,
    )
    assert "u-agg" in result.omitted_unit_ids
    assert "u-agg" not in result.included_unit_ids


@pytest.mark.asyncio
async def test_required_condition_false_blocks(tmp_path: Path):
    store = FileArtifactStore(tmp_path)
    plan = _plan()
    state = TaskExecutionState.from_plan(plan)
    source = create_artifact(
        FinalAnswerArtifact(answer="ok", source_node="s1"),
        producer_node_id="s1",
        task_id="t51",
    )
    await store.put(source)
    state.subtasks["s1"].status = SubtaskStatus.COMMITTED
    state.subtasks["s1"].final_output_artifact_id = source.artifact_id
    state.subtasks["s2"].status = SubtaskStatus.READY
    state.communication_plan = CommunicationPlan(
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
        delivery_schedule=[
            DeliveryRule(
                rule_id="r1",
                payload_id="p1",
                enabled=True,
                condition=DeliveryCondition(kind="never"),
            )
        ],
    )
    result = await CommunicationDeliveryEngine(store).deliver_for_target(
        task_plan=plan,
        task_state=state,
        communication_plan=state.communication_plan,
        target_subtask_id="s2",
    )
    assert result.blocked is True
    assert result.block_reason is DeliveryFailureReason.REQUIRED_CONDITION_UNSATISFIED


@pytest.mark.asyncio
async def test_optional_condition_false_skips(tmp_path: Path):
    store = FileArtifactStore(tmp_path)
    plan = _plan()
    state = TaskExecutionState.from_plan(plan)
    source = create_artifact(
        FinalAnswerArtifact(answer="ok", source_node="s1"),
        producer_node_id="s1",
        task_id="t51",
    )
    await store.put(source)
    state.subtasks["s1"].status = SubtaskStatus.COMMITTED
    state.subtasks["s1"].final_output_artifact_id = source.artifact_id
    state.subtasks["s2"].status = SubtaskStatus.READY
    state.communication_plan = CommunicationPlan(
        version=1,
        payload_contracts=[
            PayloadContract(
                payload_id="p1",
                source_subtask_id="s1",
                target_subtask_id="s2",
                artifact_type="FinalAnswerArtifact",
                required=False,
            )
        ],
        delivery_schedule=[
            DeliveryRule(
                rule_id="r1",
                payload_id="p1",
                enabled=True,
                condition=DeliveryCondition(kind="never"),
            )
        ],
        context_budgets={"s2": 10_000},
    )
    result = await CommunicationDeliveryEngine(store).deliver_for_target(
        task_plan=plan,
        task_state=state,
        communication_plan=state.communication_plan,
        target_subtask_id="s2",
    )
    assert result.blocked is False
    assert result.delivered_slots == {}
    assert any(
        r.status is DeliveryStatus.SKIPPED_CONDITION_FALSE for r in result.audit_records
    )


@pytest.mark.asyncio
async def test_delivery_rule_fallback_selects_satisfied_rule(tmp_path: Path):
    store = FileArtifactStore(tmp_path)
    plan = _plan()
    state = TaskExecutionState.from_plan(plan)
    source = create_artifact(
        FinalAnswerArtifact(answer="ok", source_node="s1"),
        producer_node_id="s1",
        task_id="t51",
    )
    await store.put(source)
    state.subtasks["s1"].status = SubtaskStatus.COMMITTED
    state.subtasks["s1"].final_output_artifact_id = source.artifact_id
    state.subtasks["s2"].status = SubtaskStatus.READY
    state.communication_plan = CommunicationPlan(
        version=1,
        payload_contracts=[
            PayloadContract(
                payload_id="p1",
                source_subtask_id="s1",
                target_subtask_id="s2",
                artifact_type="FinalAnswerArtifact",
                required=True,
                metadata={"slot": "comm:p1"},
            )
        ],
        delivery_schedule=[
            DeliveryRule(
                rule_id="r-high",
                payload_id="p1",
                priority=10,
                condition=DeliveryCondition(kind="never"),
            ),
            DeliveryRule(
                rule_id="r-low",
                payload_id="p1",
                priority=1,
                trigger=DeliveryTrigger.ON_SOURCE_COMMIT,
            ),
        ],
        context_budgets={"s2": 10_000},
    )
    result = await CommunicationDeliveryEngine(store).deliver_for_target(
        task_plan=plan,
        task_state=state,
        communication_plan=state.communication_plan,
        target_subtask_id="s2",
    )
    assert result.blocked is False
    assert "comm:p1" in result.delivered_slots
    assert any(r.rule_id == "r-low" for r in result.new_records)


@pytest.mark.asyncio
async def test_delivery_preflight_runs_before_lease(tmp_path: Path):
    store = FileArtifactStore(tmp_path)
    plan = _plan()
    state = TaskExecutionState.from_plan(plan)
    state.subtasks["s1"].status = SubtaskStatus.PENDING
    state.subtasks["s2"].status = SubtaskStatus.READY
    state.communication_plan = CommunicationPlan(
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
        delivery_schedule=[DeliveryRule(rule_id="r1", payload_id="p1")],
    )
    pre = await CommunicationDeliveryEngine(store).preflight_for_target(
        task_plan=plan,
        task_state=state,
        communication_plan=state.communication_plan,
        target_subtask_id="s2",
    )
    assert pre.deliverable is False
    assert pre.blocked is True
    assert pre.block_reason is DeliveryFailureReason.REQUIRED_SOURCE_NOT_COMMITTED


@pytest.mark.asyncio
async def test_crash_before_promote_keeps_old_active(tmp_path: Path):
    plan = _plan()
    state = TaskExecutionState.from_plan(plan)
    state.subtasks["s2"].status = SubtaskStatus.PENDING
    old_hash = state.task_plan.content_hash()
    new_plan, new_comm, policy, _ = apply_global_edits(
        task_plan=plan,
        communication_plan=state.communication_plan,
        scheduling_policy=TaskSchedulingPolicy(),
        edits=[ContextBudgetEdit(target_subtask_id="s2", max_tokens=21)],
        eligible_subtask_ids={"s2"},
    )
    rev = GlobalPlanRevision(
        revision_id="rev-crash-a",
        revision_number=1,
        parent_plan_hash=plan.content_hash(),
        new_plan_hash=new_plan.content_hash(),
        parent_communication_hash="p",
        new_communication_hash="n",
        edits=[ContextBudgetEdit(target_subtask_id="s2", max_tokens=21)],
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

    def _boom() -> None:
        raise RuntimeError("crash before promote")

    with pytest.raises(RuntimeError, match="crash before promote"):
        await commit_prepared_revision(
            state=state,
            prepared=prepared,
            checkpoint_store=TaskCheckpointStore(tmp_path),
            run_dir=tmp_path,
            hooks=RevisionTransactionHooks(after_staging_fsync=_boom),
        )
    assert state.task_plan.content_hash() == old_hash
    assert state.active_plan_revision_id is None
    assert not (tmp_path / "plan_revisions" / "rev-crash-a").exists()


@pytest.mark.asyncio
async def test_crash_after_checkpoint_before_live_swap_recovers_active(tmp_path: Path):
    plan = _plan()
    state = TaskExecutionState.from_plan(plan)
    state.subtasks["s2"].status = SubtaskStatus.PENDING
    store = TaskCheckpointStore(tmp_path)
    new_plan, new_comm, policy, _ = apply_global_edits(
        task_plan=plan,
        communication_plan=state.communication_plan,
        scheduling_policy=TaskSchedulingPolicy(),
        edits=[ContextBudgetEdit(target_subtask_id="s2", max_tokens=22)],
        eligible_subtask_ids={"s2"},
    )
    rev = GlobalPlanRevision(
        revision_id="rev-crash-c",
        revision_number=1,
        parent_plan_hash=plan.content_hash(),
        new_plan_hash=new_plan.content_hash(),
        parent_communication_hash="p",
        new_communication_hash="n",
        edits=[ContextBudgetEdit(target_subtask_id="s2", max_tokens=22)],
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

    def _boom() -> None:
        raise RuntimeError("crash before live swap")

    with pytest.raises(RuntimeError, match="crash before live swap"):
        await commit_prepared_revision(
            state=state,
            prepared=prepared,
            checkpoint_store=store,
            run_dir=tmp_path,
            hooks=RevisionTransactionHooks(after_checkpoint_save=_boom),
        )
    # Live object not swapped, but active checkpoint already points at new revision.
    assert state.active_plan_revision_id is None
    loaded = await store.load(state.task_id)
    assert loaded is not None
    assert loaded.active_plan_revision_id == "rev-crash-c"
    assert loaded.global_revision == 1
    verify_checkpoint_revision_consistency(state=loaded, run_dir=tmp_path)


@pytest.mark.asyncio
async def test_revision_retry_idempotent_same_hash(tmp_path: Path):
    plan = _plan()
    state = TaskExecutionState.from_plan(plan)
    state.subtasks["s2"].status = SubtaskStatus.PENDING
    new_plan, new_comm, policy, _ = apply_global_edits(
        task_plan=plan,
        communication_plan=state.communication_plan,
        scheduling_policy=TaskSchedulingPolicy(),
        edits=[ContextBudgetEdit(target_subtask_id="s2", max_tokens=23)],
        eligible_subtask_ids={"s2"},
    )
    rev = GlobalPlanRevision(
        revision_id="rev-crash-d",
        revision_number=1,
        parent_plan_hash=plan.content_hash(),
        new_plan_hash=new_plan.content_hash(),
        parent_communication_hash="p",
        new_communication_hash="n",
        edits=[ContextBudgetEdit(target_subtask_id="s2", max_tokens=23)],
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

    class Boom(TaskCheckpointStore):
        async def save(self, st):  # noqa: ANN001
            raise RuntimeError("orphan after promote")

    with pytest.raises(RuntimeError):
        await commit_prepared_revision(
            state=state,
            prepared=prepared,
            checkpoint_store=Boom(tmp_path),
            run_dir=tmp_path,
        )
    assert state.active_plan_revision_id is None
    # Retry with identical hash reuses promoted final (idempotent promote).
    prepared2 = prepare_revision_staging(
        run_dir=tmp_path,
        revision=rev,
        new_plan=new_plan,
        new_communication=new_comm,
        scheduling_policy=policy,
        state=state,
    )
    await commit_prepared_revision(
        state=state,
        prepared=prepared2,
        checkpoint_store=TaskCheckpointStore(tmp_path),
        run_dir=tmp_path,
    )
    assert state.active_plan_revision_id == "rev-crash-d"
    assert state.global_revision == 1


@pytest.mark.asyncio
async def test_blocked_target_does_not_consume_lease(tmp_path: Path):
    from orchestra.control.input_assembler import SubtaskInputAssembler
    from orchestra.control.ready_scheduler import ReadySubtaskScheduler
    from orchestra.ir.artifacts import ArtifactBundle

    store = FileArtifactStore(tmp_path)
    plan = TaskPlan(
        task_id="t51",
        plan_version=1,
        decomposition_rationale="lease",
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
                priority=10,
            ),
            SubtaskSpec(
                subtask_id="s3",
                title="s3",
                objective="ok",
                dependencies=[],
                keystone_harness_id="repository_test_harness",
                local_graph_template=GRAPH,
                budget=BudgetSpec(),
                priority=1,
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
            delivery_schedule=[DeliveryRule(rule_id="r1", payload_id="p1")],
        ),
    )
    state = TaskExecutionState.from_plan(plan)
    state.communication_plan = plan.communication_plan
    state.scheduling_policy = TaskSchedulingPolicy(max_concurrent_subtasks=1)
    # s1 committed without artifact → s2 dependency-ready but communication-blocked.
    state.subtasks["s1"].status = SubtaskStatus.COMMITTED
    state.subtasks["s2"].status = SubtaskStatus.READY
    state.subtasks["s3"].status = SubtaskStatus.READY
    sched = ReadySubtaskScheduler.__new__(ReadySubtaskScheduler)
    sched.max_concurrent_subtasks = 1
    sched.input_assembler = SubtaskInputAssembler(store)
    deliverable = await sched._deliverable_ready_ids(
        state=state,
        task_plan=plan,
        initial_artifacts=ArtifactBundle(),
    )
    assert deliverable == ["s3"]
    assert state.subtasks["s2"].communication_block_reason
    assert state.subtasks["s2"].lease_status != "leased"
    assert state.subtasks["s3"].lease_status != "leased"


@pytest.mark.asyncio
async def test_revision_graph_hash_verified_on_recovery(tmp_path: Path):
    plan = _plan()
    state = TaskExecutionState.from_plan(plan)
    state.subtasks["s2"].status = SubtaskStatus.PENDING
    new_plan, new_comm, policy, _ = apply_global_edits(
        task_plan=plan,
        communication_plan=state.communication_plan,
        scheduling_policy=TaskSchedulingPolicy(),
        edits=[
            PendingBackendAssignmentEdit(
                subtask_id="s2",
                node_id="codex_implementer",
                backend_id="structured_llm",
            )
        ],
        eligible_subtask_ids={"s2"},
    )
    rev = GlobalPlanRevision(
        revision_id="rev-hash-1",
        revision_number=1,
        parent_plan_hash=plan.content_hash(),
        new_plan_hash=new_plan.content_hash(),
        parent_communication_hash="p",
        new_communication_hash="n",
        edits=[
            PendingBackendAssignmentEdit(
                subtask_id="s2",
                node_id="codex_implementer",
                backend_id="structured_llm",
            )
        ],
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
        allowed_backend_pools={"default": ["structured_llm", "codex_sdk"]},
    )
    await commit_prepared_revision(
        state=state,
        prepared=prepared,
        checkpoint_store=TaskCheckpointStore(tmp_path),
        run_dir=tmp_path,
    )
    verify_checkpoint_revision_consistency(state=state, run_dir=tmp_path)
    s2 = state.subtasks["s2"].spec
    path = Path(str(s2.metadata["materialized_graph_path"]))
    assert path.exists()
    assert ".staging-" not in str(path)
