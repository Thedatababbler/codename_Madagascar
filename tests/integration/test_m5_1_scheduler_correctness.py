"""M5.1 scheduler-level correctness: paths, crash, aggregation budget, preflight."""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestra.communication.aggregation import AggregationRule, AggregationStrategy
from orchestra.communication.budget import FinalDeliveryUnit, pack_final_delivery_units
from orchestra.communication.delivery import CommunicationDeliveryEngine
from orchestra.communication.ledger import DeliveryFailureReason
from orchestra.communication.payload import (
    DeliveryCondition,
    DeliveryRule,
    PayloadContract,
)
from orchestra.communication.plan import CommunicationPlan
from orchestra.communication.projection import estimate_tokens
from orchestra.control.slow_loop.revision import (
    RevisionTransactionHooks,
    commit_prepared_revision,
    prepare_revision_staging,
)
from orchestra.control.slow_loop.schemas import (
    ContextBudgetEdit,
    GlobalPlanRevision,
    PendingBackendAssignmentEdit,
    TaskSchedulingPolicy,
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
        run_id="m51",
        task_id="m51",
        run_dir=tmp_path,
        limits=limits,
        semaphores=RuntimeSemaphores(limits),
        contract_hash="c",
    )


def _plan() -> TaskPlan:
    return TaskPlan(
        task_id="m51",
        plan_version=1,
        decomposition_rationale="m51",
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
    )


@pytest.mark.asyncio
async def test_committed_revision_graph_path_has_no_staging(tmp_path: Path):
    from orchestra.control.slow_loop.edits import apply_global_edits

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
        revision_id="rev-snap-1",
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
    path = Path(str(state.subtasks["s2"].spec.metadata["materialized_graph_path"]))
    assert path.exists()
    assert ".staging-" not in str(path)
    text = path.read_text(encoding="utf-8")
    assert "structured_llm" in text


@pytest.mark.asyncio
async def test_crash_after_promote_before_checkpoint_keeps_old_active(tmp_path: Path):
    from orchestra.control.slow_loop.edits import apply_global_edits

    plan = _plan()
    state = TaskExecutionState.from_plan(plan)
    state.subtasks["s2"].status = SubtaskStatus.PENDING
    old_hash = state.task_plan.content_hash()
    new_plan, new_comm, policy, _ = apply_global_edits(
        task_plan=plan,
        communication_plan=state.communication_plan,
        scheduling_policy=TaskSchedulingPolicy(),
        edits=[ContextBudgetEdit(target_subtask_id="s2", max_tokens=42)],
        eligible_subtask_ids={"s2"},
    )
    rev = GlobalPlanRevision(
        revision_id="rev-crash-b",
        revision_number=1,
        parent_plan_hash=plan.content_hash(),
        new_plan_hash=new_plan.content_hash(),
        parent_communication_hash="p",
        new_communication_hash="n",
        edits=[ContextBudgetEdit(target_subtask_id="s2", max_tokens=42)],
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
            raise RuntimeError("checkpoint crash")

    with pytest.raises(RuntimeError):
        await commit_prepared_revision(
            state=state,
            prepared=prepared,
            checkpoint_store=Boom(tmp_path),
            run_dir=tmp_path,
            hooks=RevisionTransactionHooks(),
        )
    assert state.task_plan.content_hash() == old_hash
    assert state.active_plan_revision_id is None
    assert (tmp_path / "plan_revisions" / "rev-crash-b").exists()


@pytest.mark.asyncio
async def test_aggregate_budget_blocks_oversized_final_unit(tmp_path: Path):
    store = FileArtifactStore(tmp_path)
    plan = TaskPlan(
        task_id="m51",
        plan_version=1,
        decomposition_rationale="agg",
        subtasks=[
            SubtaskSpec(
                subtask_id="s1",
                title="s1",
                objective="a",
                dependencies=[],
                keystone_harness_id="h",
                local_graph_template=GRAPH,
                budget=BudgetSpec(),
            ),
            SubtaskSpec(
                subtask_id="s2",
                title="s2",
                objective="b",
                dependencies=[],
                keystone_harness_id="h",
                local_graph_template=GRAPH,
                budget=BudgetSpec(),
            ),
            SubtaskSpec(
                subtask_id="s3",
                title="s3",
                objective="c",
                dependencies=["s1", "s2"],
                keystone_harness_id="h",
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
                    target_subtask_id="s3",
                    artifact_type="FinalAnswerArtifact",
                    required=True,
                    max_tokens=50_000,
                    metadata={"slot": "shared"},
                ),
                PayloadContract(
                    payload_id="p2",
                    source_subtask_id="s2",
                    target_subtask_id="s3",
                    artifact_type="FinalAnswerArtifact",
                    required=True,
                    max_tokens=50_000,
                    metadata={"slot": "shared"},
                ),
            ],
            delivery_schedule=[
                DeliveryRule(rule_id="r1", payload_id="p1"),
                DeliveryRule(rule_id="r2", payload_id="p2"),
            ],
            aggregation_rules=[
                AggregationRule(
                    rule_id="agg1",
                    source_payload_ids=["p1", "p2"],
                    strategy=AggregationStrategy.CONCAT_TEXT,
                    delimiter="\n",
                    max_tokens=50_000,
                    target_slot="shared",
                    metadata={"target_subtask_id": "s3", "slot": "shared"},
                )
            ],
            # Tiny target budget: final aggregate artifact cannot fit.
            context_budgets={"s3": 20},
        ),
    )
    state = TaskExecutionState.from_plan(plan)
    state.communication_plan = plan.communication_plan
    for sid, text in [("s1", "A" * 400), ("s2", "B" * 400)]:
        art = create_artifact(
            FinalAnswerArtifact(answer=text, source_node=sid),
            producer_node_id=sid,
            task_id="m51",
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
    assert result.block_reason is DeliveryFailureReason.CONTEXT_BUDGET_INFEASIBLE
    assert "shared" not in result.delivered_slots
    # Expanding the target budget allows the same final aggregate unit.
    plan.communication_plan.context_budgets["s3"] = 10_000
    state.communication_plan = plan.communication_plan
    ok = await CommunicationDeliveryEngine(store).deliver_for_target(
        task_plan=plan,
        task_state=state,
        communication_plan=state.communication_plan,
        target_subtask_id="s3",
    )
    assert ok.blocked is False
    assert "shared" in ok.delivered_slots


@pytest.mark.asyncio
async def test_required_condition_false_preflight_blocks(tmp_path: Path):
    store = FileArtifactStore(tmp_path)
    plan = _plan()
    state = TaskExecutionState.from_plan(plan)
    source = create_artifact(
        FinalAnswerArtifact(answer="x", source_node="s1"),
        producer_node_id="s1",
        task_id="m51",
    )
    await store.put(source)
    state.subtasks["s1"].status = SubtaskStatus.COMMITTED
    state.subtasks["s1"].final_output_artifact_id = source.artifact_id
    state.subtasks["s2"].status = SubtaskStatus.READY
    state.subtasks["s3"].status = SubtaskStatus.READY
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
                condition=DeliveryCondition(kind="never"),
            )
        ],
    )
    engine = CommunicationDeliveryEngine(store)
    pre_s2 = await engine.preflight_for_target(
        task_plan=plan,
        task_state=state,
        communication_plan=state.communication_plan,
        target_subtask_id="s2",
    )
    pre_s3 = await engine.preflight_for_target(
        task_plan=plan,
        task_state=state,
        communication_plan=state.communication_plan,
        target_subtask_id="s3",
    )
    assert pre_s2.blocked is True
    assert pre_s2.block_reason is DeliveryFailureReason.REQUIRED_CONDITION_UNSATISFIED
    assert pre_s3.deliverable is True
    # Blocked target must not be leased; clear sibling remains deliverable.
    state.subtasks["s2"].communication_block_reason = pre_s2.block_reason.value
    assert state.subtasks["s2"].communication_block_reason
    assert state.subtasks["s3"].communication_block_reason is None


@pytest.mark.asyncio
async def test_restart_loads_materialized_graph_path(tmp_path: Path):
    from orchestra.control.slow_loop.edits import apply_global_edits

    plan = _plan()
    state = TaskExecutionState.from_plan(plan)
    state.subtasks["s2"].status = SubtaskStatus.PENDING
    store = TaskCheckpointStore(tmp_path)
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
        revision_id="rev-restart-1",
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
        checkpoint_store=store,
        run_dir=tmp_path,
    )
    loaded = await store.load(state.task_id)
    assert loaded is not None
    path = Path(str(loaded.subtasks["s2"].spec.metadata["materialized_graph_path"]))
    assert path.exists()
    assert ".staging-" not in str(path)
    assert "structured_llm" in path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_optional_aggregate_excluded_never_delivered(tmp_path: Path):
    store = FileArtifactStore(tmp_path)
    plan = TaskPlan(
        task_id="m51",
        plan_version=1,
        decomposition_rationale="opt-agg",
        subtasks=[
            SubtaskSpec(
                subtask_id="s1",
                title="s1",
                objective="a",
                dependencies=[],
                keystone_harness_id="h",
                local_graph_template=GRAPH,
                budget=BudgetSpec(),
            ),
            SubtaskSpec(
                subtask_id="s2",
                title="s2",
                objective="b",
                dependencies=[],
                keystone_harness_id="h",
                local_graph_template=GRAPH,
                budget=BudgetSpec(),
            ),
            SubtaskSpec(
                subtask_id="s3",
                title="s3",
                objective="c",
                dependencies=["s1", "s2"],
                keystone_harness_id="h",
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
                    target_subtask_id="s3",
                    artifact_type="FinalAnswerArtifact",
                    required=False,
                    max_tokens=50_000,
                    metadata={"slot": "shared"},
                ),
                PayloadContract(
                    payload_id="p2",
                    source_subtask_id="s2",
                    target_subtask_id="s3",
                    artifact_type="FinalAnswerArtifact",
                    required=False,
                    max_tokens=50_000,
                    metadata={"slot": "shared"},
                ),
            ],
            delivery_schedule=[
                DeliveryRule(rule_id="r1", payload_id="p1"),
                DeliveryRule(rule_id="r2", payload_id="p2"),
            ],
            aggregation_rules=[
                AggregationRule(
                    rule_id="agg1",
                    source_payload_ids=["p1", "p2"],
                    strategy=AggregationStrategy.CONCAT_TEXT,
                    delimiter="\n",
                    max_tokens=50_000,
                    target_slot="shared",
                    metadata={"target_subtask_id": "s3", "slot": "shared"},
                )
            ],
            context_budgets={"s3": 10},
        ),
    )
    state = TaskExecutionState.from_plan(plan)
    state.communication_plan = plan.communication_plan
    for sid, text in [("s1", "A" * 400), ("s2", "B" * 400)]:
        art = create_artifact(
            FinalAnswerArtifact(answer=text, source_node=sid),
            producer_node_id=sid,
            task_id="m51",
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
    assert result.blocked is False
    assert "shared" not in result.delivered_slots
    assert not any(
        r.payload_id == "agg:agg1" and r.status.value == "delivered"
        for r in result.new_records
    )


def test_pack_final_units_excludes_aggregate():
    art = create_artifact(
        FinalAnswerArtifact(answer="huge-" + ("z" * 5000), source_node="agg"),
        producer_node_id="agg",
        task_id="m51",
    )
    unit = FinalDeliveryUnit(
        delivery_unit_id="agg-unit",
        target_subtask_id="s3",
        target_slot="shared",
        artifact=art,
        estimated_tokens=estimate_tokens(art.payload),
        required=False,
        aggregation_rule_id="agg1",
    )
    result = pack_final_delivery_units(
        target_subtask_id="s3",
        max_tokens=5,
        units=[unit],
        fail_closed_on_required=False,
    )
    assert "agg-unit" in result.omitted_unit_ids
