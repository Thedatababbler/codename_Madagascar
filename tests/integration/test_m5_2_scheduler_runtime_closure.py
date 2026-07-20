"""M5.2 integration: chained delivery, slow-loop after history, budget wiring."""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestra.communication.delivery import CommunicationDeliveryEngine
from orchestra.communication.payload import DeliveryRule, PayloadContract
from orchestra.communication.plan import CommunicationPlan
from orchestra.control.slow_loop.controller import SlowLoopController
from orchestra.control.slow_loop.edits import apply_global_edits
from orchestra.control.slow_loop.revision import commit_prepared_revision, prepare_revision_staging
from orchestra.control.slow_loop.schemas import (
    ContextBudgetEdit,
    GlobalPlanRevision,
    PendingBackendAssignmentEdit,
    SlowLoopConfig,
    TaskSchedulingPolicy,
    UpsertPayloadContractEdit,
)
from orchestra.control.slow_loop.task_budget import TaskBudgetTracker
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
        run_id="m52",
        task_id="m52",
        run_dir=tmp_path,
        limits=limits,
        semaphores=RuntimeSemaphores(limits),
        contract_hash="c",
    )


def _chain() -> TaskPlan:
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
                DeliveryRule(rule_id="r12", payload_id="p12"),
                DeliveryRule(rule_id="r23", payload_id="p23"),
            ],
            context_budgets={"s2": 10_000, "s3": 10_000},
        ),
        metadata={"task_budget": {"max_backend_calls": 20}},
    )


@pytest.mark.asyncio
async def test_chained_s1_s2_s3_delivery_with_historical_contract(tmp_path: Path):
    store = FileArtifactStore(tmp_path)
    plan = _chain()
    state = TaskExecutionState.from_plan(plan)
    state.communication_plan = plan.communication_plan
    engine = CommunicationDeliveryEngine(store)
    a1 = create_artifact(
        FinalAnswerArtifact(answer="s1", source_node="s1"),
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
    state.delivery_ledger.extend(d2.new_records)
    a2 = create_artifact(
        FinalAnswerArtifact(answer="s2", source_node="s2"),
        producer_node_id="s2",
        task_id="m52",
    )
    await store.put(a2)
    state.subtasks["s2"].status = SubtaskStatus.COMMITTED
    state.subtasks["s2"].final_output_artifact_id = a2.artifact_id
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
    assert any(r.payload_id == "p12" for r in state.delivery_ledger)
    assert any(r.payload_id == "p23" for r in state.delivery_ledger)


@pytest.mark.asyncio
async def test_slow_loop_revision_after_historical_delivery(tmp_path: Path):
    store = FileArtifactStore(tmp_path)
    ckpt = TaskCheckpointStore(tmp_path)
    plan = _chain()
    state = TaskExecutionState.from_plan(plan)
    state.communication_plan = plan.communication_plan
    engine = CommunicationDeliveryEngine(store)
    a1 = create_artifact(
        FinalAnswerArtifact(answer="A", source_node="s1"),
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
    state.delivery_ledger.extend(d2.new_records)
    a2 = create_artifact(
        FinalAnswerArtifact(answer="B", source_node="s2"),
        producer_node_id="s2",
        task_id="m52",
    )
    await store.put(a2)
    state.subtasks["s2"].status = SubtaskStatus.COMMITTED
    state.subtasks["s2"].final_output_artifact_id = a2.artifact_id
    state.subtasks["s3"].status = SubtaskStatus.PENDING
    # Slow Loop edits future S3 communication only.
    new_contract = PayloadContract(
        payload_id="p23",
        source_subtask_id="s2",
        target_subtask_id="s3",
        artifact_type="FinalAnswerArtifact",
        required=True,
        max_tokens=128,
        metadata={"slot": "comm:p23", "tuned": True},
    )
    new_plan, new_comm, policy, _ = apply_global_edits(
        task_plan=plan,
        communication_plan=state.communication_plan,
        scheduling_policy=TaskSchedulingPolicy(),
        edits=[UpsertPayloadContractEdit(contract=new_contract)],
        eligible_subtask_ids={"s3"},
    )
    rev = GlobalPlanRevision(
        revision_id="rev-hist-1",
        revision_number=1,
        parent_plan_hash=plan.content_hash(),
        new_plan_hash=new_plan.content_hash(),
        parent_communication_hash="p",
        new_communication_hash="n",
        edits=[UpsertPayloadContractEdit(contract=new_contract)],
        eligible_subtask_ids=["s3"],
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
        checkpoint_store=ckpt,
        run_dir=tmp_path,
    )
    assert state.active_plan_revision_id == "rev-hist-1"
    assert any(c.payload_id == "p12" for c in state.communication_plan.payload_contracts)
    p23 = next(c for c in state.communication_plan.payload_contracts if c.payload_id == "p23")
    assert p23.max_tokens == 128


@pytest.mark.asyncio
async def test_real_backend_adaptation_materializes_codex_node(tmp_path: Path):
    plan = _chain()
    state = TaskExecutionState.from_plan(plan)
    state.subtasks["s1"].status = SubtaskStatus.COMMITTED
    state.subtasks["s2"].status = SubtaskStatus.PENDING
    state.subtasks["s3"].status = SubtaskStatus.PENDING
    edit = PendingBackendAssignmentEdit(
        subtask_id="s3",
        node_id="codex_implementer",
        backend_id="structured_llm",
        model_name="gpt-4o-mini",
    )
    new_plan, new_comm, policy, _ = apply_global_edits(
        task_plan=plan,
        communication_plan=state.communication_plan,
        scheduling_policy=TaskSchedulingPolicy(),
        edits=[edit],
        eligible_subtask_ids={"s3"},
    )
    rev = GlobalPlanRevision(
        revision_id="rev-be-1",
        revision_number=1,
        parent_plan_hash=plan.content_hash(),
        new_plan_hash=new_plan.content_hash(),
        parent_communication_hash="p",
        new_communication_hash="n",
        edits=[edit],
        eligible_subtask_ids=["s3"],
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
        backend_model_pools={"structured_llm": ["gpt-4o-mini"]},
    )
    await commit_prepared_revision(
        state=state,
        prepared=prepared,
        checkpoint_store=TaskCheckpointStore(tmp_path),
        run_dir=tmp_path,
    )
    path = Path(str(state.subtasks["s3"].spec.metadata["materialized_graph_path"]))
    assert path.exists()
    assert "structured_llm" in path.read_text(encoding="utf-8")
    assert "__future_agent__" not in path.read_text(encoding="utf-8")
    # Original template unchanged.
    assert "codex_sdk" in Path(GRAPH).read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_scheduler_budget_snapshot_reaches_slow_loop(tmp_path: Path):
    plan = _chain()
    state = TaskExecutionState.from_plan(plan)
    state.subtasks["s1"].status = SubtaskStatus.COMMITTED
    state.subtasks["s2"].status = SubtaskStatus.PENDING
    state.committed_subtask_count = 1
    tracker = TaskBudgetTracker(max_backend_calls=1)
    budget = tracker.snapshot(task_plan=plan, task_state=state)
    assert budget.budget_configured is True
    assert budget.ratio < 0.35
    ctrl = SlowLoopController(
        config=SlowLoopConfig(
            enabled=True,
            allowed_backend_assignments={"default": ["structured_llm", "codex_sdk"]},
            backend_model_pools={"structured_llm": ["gpt-4o-mini"]},
        ),
        checkpoint_store=TaskCheckpointStore(tmp_path),
    )
    result = await ctrl.maybe_update(
        task_plan=plan,
        state=state,
        context=_ctx(tmp_path),
        leased_subtask_ids=set(),
        task_budget=budget,
    )
    # Budget pressure should produce a diagnosis path (update or audited no-safe).
    assert result.trigger_reasons
    assert any(t.value == "budget_pressure" for t in result.trigger_reasons)


@pytest.mark.asyncio
async def test_checkpoint_resume_keeps_historical_ledger(tmp_path: Path):
    store = FileArtifactStore(tmp_path / "arts")
    ckpt = TaskCheckpointStore(tmp_path)
    plan = _chain()
    state = TaskExecutionState.from_plan(plan)
    state.communication_plan = plan.communication_plan
    engine = CommunicationDeliveryEngine(store)
    a1 = create_artifact(
        FinalAnswerArtifact(answer="s1", source_node="s1"),
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
    state.delivery_ledger.extend(d2.new_records)
    state.subtasks["s2"].status = SubtaskStatus.COMMITTED
    a2 = create_artifact(
        FinalAnswerArtifact(answer="s2", source_node="s2"),
        producer_node_id="s2",
        task_id="m52",
    )
    await store.put(a2)
    state.subtasks["s2"].final_output_artifact_id = a2.artifact_id
    # Apply a tiny future revision on S3 context budget.
    new_plan, new_comm, policy, _ = apply_global_edits(
        task_plan=plan,
        communication_plan=state.communication_plan,
        scheduling_policy=TaskSchedulingPolicy(),
        edits=[ContextBudgetEdit(target_subtask_id="s3", max_tokens=9000)],
        eligible_subtask_ids={"s3"},
    )
    rev = GlobalPlanRevision(
        revision_id="rev-resume-1",
        revision_number=1,
        parent_plan_hash=plan.content_hash(),
        new_plan_hash=new_plan.content_hash(),
        parent_communication_hash="p",
        new_communication_hash="n",
        edits=[ContextBudgetEdit(target_subtask_id="s3", max_tokens=9000)],
        eligible_subtask_ids=["s3"],
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
        checkpoint_store=ckpt,
        run_dir=tmp_path,
    )
    loaded = await ckpt.load("m52")
    assert loaded is not None
    assert loaded.active_plan_revision_id == "rev-resume-1"
    assert any(r.payload_id == "p12" for r in loaded.delivery_ledger)
    loaded.subtasks["s3"].status = SubtaskStatus.READY
    d3 = await engine.deliver_for_target(
        task_plan=loaded.task_plan,
        task_state=loaded,
        communication_plan=loaded.communication_plan,
        target_subtask_id="s3",
    )
    assert d3.blocked is False
    # Second delivery does not duplicate S3 records.
    loaded.delivery_ledger.extend(d3.new_records)
    d3b = await engine.deliver_for_target(
        task_plan=loaded.task_plan,
        task_state=loaded,
        communication_plan=loaded.communication_plan,
        target_subtask_id="s3",
    )
    assert not d3b.new_records
