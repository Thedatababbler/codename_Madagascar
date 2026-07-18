"""Scheduler-level M5 Final Hardening coverage (delivery, revision, materialization)."""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestra.backends.base import ModelSpec
from orchestra.communication.aggregation import AggregationRule, AggregationStrategy
from orchestra.communication.delivery import CommunicationDeliveryEngine
from orchestra.communication.ledger import DeliveryFailureReason, DeliveryStatus
from orchestra.communication.payload import (
    DeliveryCondition,
    DeliveryRule,
    DeliveryTrigger,
    PayloadContract,
)
from orchestra.communication.plan import CommunicationPlan
from orchestra.control.input_assembler import (
    CommunicationDeliveryBlocked,
    SubtaskInputAssembler,
)
from orchestra.control.slow_loop.controller import SlowLoopController
from orchestra.control.slow_loop.edits import apply_global_edits
from orchestra.control.slow_loop.graph_materializer import FutureGraphMaterializer
from orchestra.control.slow_loop.revision import (
    commit_prepared_revision,
    prepare_revision_staging,
)
from orchestra.control.slow_loop.schemas import (
    ContextBudgetEdit,
    GlobalPlanRevision,
    GlobalPlanRevisionStatus,
    PendingBackendAssignmentEdit,
    SlowLoopBudget,
    SlowLoopConfig,
    TaskSchedulingPolicy,
)
from orchestra.control.slow_loop.validation import FuturePlanValidator
from orchestra.control.task_state import SubtaskStatus, TaskExecutionState
from orchestra.decomposition.schemas import BudgetSpec, SubtaskSpec, TaskPlan
from orchestra.ir.artifacts import ArtifactBundle, create_artifact
from orchestra.ir.nodes import AgentNodeSpec
from orchestra.runtime.backend import RunContext
from orchestra.runtime.limits import RuntimeLimits, RuntimeSemaphores
from orchestra.runtime.task_checkpoint import TaskCheckpointStore
from orchestra.schemas.artifacts import FinalAnswerArtifact
from orchestra.storage.artifacts import FileArtifactStore

GRAPH = "configs/graphs/codex_single_implementer.yaml"


def _ctx(tmp_path: Path, task_id: str = "m5h") -> RunContext:
    limits = RuntimeLimits(
        max_parallel_benchmark_tasks=1,
        max_parallel_nodes_per_task=2,
        max_parallel_llm_calls=2,
        max_parallel_sandboxes=2,
    )
    return RunContext(
        run_id="m5h",
        task_id=task_id,
        run_dir=tmp_path,
        limits=limits,
        semaphores=RuntimeSemaphores(limits),
        contract_hash="c",
    )


def _two_subtask_plan(*, required: bool = False) -> TaskPlan:
    return TaskPlan(
        task_id="m5h",
        plan_version=1,
        decomposition_rationale="m5h",
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
        ],
        communication_plan=CommunicationPlan(
            version=1,
            payload_contracts=[
                PayloadContract(
                    payload_id="p1",
                    source_subtask_id="s1",
                    target_subtask_id="s2",
                    artifact_type="FinalAnswerArtifact",
                    required=required,
                    required_fields=["answer"],
                    max_tokens=2048,
                    metadata={"slot": "comm:p1", "required": required},
                )
            ],
            delivery_schedule=[
                DeliveryRule(
                    rule_id="r1",
                    payload_id="p1",
                    trigger=DeliveryTrigger.ON_SOURCE_COMMIT,
                    enabled=True,
                )
            ],
            context_budgets={"s2": 10_000},
        ),
    )


@pytest.mark.asyncio
async def test_a_delivery_rule_controls_delivery(tmp_path):
    store = FileArtifactStore(tmp_path)
    plan = _two_subtask_plan()
    state = TaskExecutionState.from_plan(plan)
    state.communication_plan = plan.communication_plan.model_copy(
        update={
            "delivery_schedule": [
                DeliveryRule(
                    rule_id="r1",
                    payload_id="p1",
                    enabled=False,
                    trigger=DeliveryTrigger.ON_SOURCE_COMMIT,
                )
            ]
        }
    )
    source = create_artifact(
        FinalAnswerArtifact(answer="payload", source_node="s1"),
        producer_node_id="s1",
        task_id="m5h",
    )
    await store.put(source)
    state.subtasks["s1"].status = SubtaskStatus.COMMITTED
    state.subtasks["s1"].final_output_artifact_id = source.artifact_id
    state.subtasks["s2"].status = SubtaskStatus.READY
    asm = SubtaskInputAssembler(store)
    bundle = await asm.assemble(
        task_plan=plan,
        task_state=state,
        subtask=state.subtasks["s2"].spec,
        root_artifacts=ArtifactBundle(),
    )
    assert "comm:p1" not in bundle.slots

    # Enable rule → delivered.
    state.communication_plan = plan.communication_plan
    state.delivery_ledger = []
    bundle2 = await asm.assemble(
        task_plan=plan,
        task_state=state,
        subtask=state.subtasks["s2"].spec,
        root_artifacts=ArtifactBundle(),
    )
    assert "comm:p1" in bundle2.slots

    # Condition false → skipped + audited.
    state.communication_plan = plan.communication_plan.model_copy(
        update={
            "delivery_schedule": [
                DeliveryRule(
                    rule_id="r1",
                    payload_id="p1",
                    enabled=True,
                    trigger=DeliveryTrigger.ON_SOURCE_COMMIT,
                    condition=DeliveryCondition(kind="never"),
                )
            ]
        }
    )
    state.delivery_ledger = []
    bundle3 = await asm.assemble(
        task_plan=plan,
        task_state=state,
        subtask=state.subtasks["s2"].spec,
        root_artifacts=ArtifactBundle(),
    )
    assert "comm:p1" not in bundle3.slots
    assert any(
        r.status is DeliveryStatus.SKIPPED_CONDITION_FALSE for r in state.delivery_ledger
    )


@pytest.mark.asyncio
async def test_b_ledger_resume_reloads_projected_payload(tmp_path):
    store = FileArtifactStore(tmp_path)
    plan = _two_subtask_plan()
    state = TaskExecutionState.from_plan(plan)
    state.communication_plan = plan.communication_plan
    source = create_artifact(
        FinalAnswerArtifact(answer="resume-me", source_node="s1"),
        producer_node_id="s1",
        task_id="m5h",
    )
    await store.put(source)
    state.subtasks["s1"].status = SubtaskStatus.COMMITTED
    state.subtasks["s1"].final_output_artifact_id = source.artifact_id
    state.subtasks["s2"].status = SubtaskStatus.READY
    asm = SubtaskInputAssembler(store)
    first = await asm.assemble(
        task_plan=plan,
        task_state=state,
        subtask=state.subtasks["s2"].spec,
        root_artifacts=ArtifactBundle(),
    )
    assert len(state.delivery_ledger) == 1
    art_id = first.slots["comm:p1"].artifact_id
    # Crash before target execution: persist checkpoint, reload.
    ck = TaskCheckpointStore(tmp_path)
    await ck.save(state)
    loaded = await ck.load("m5h")
    assert loaded is not None
    second = await asm.assemble(
        task_plan=loaded.task_plan,
        task_state=loaded,
        subtask=loaded.subtasks["s2"].spec,
        root_artifacts=ArtifactBundle(),
    )
    assert second.slots["comm:p1"].artifact_id == art_id
    delivered = [
        r for r in loaded.delivery_ledger if r.status is DeliveryStatus.DELIVERED
    ]
    assert len(delivered) == 1


@pytest.mark.asyncio
async def test_c_required_payload_blocks_target_lease(tmp_path):
    store = FileArtifactStore(tmp_path)
    plan = _two_subtask_plan(required=True)
    state = TaskExecutionState.from_plan(plan)
    state.communication_plan = plan.communication_plan
    state.subtasks["s1"].status = SubtaskStatus.PENDING
    state.subtasks["s2"].status = SubtaskStatus.READY
    asm = SubtaskInputAssembler(store)
    with pytest.raises(CommunicationDeliveryBlocked) as exc:
        await asm.assemble(
            task_plan=plan,
            task_state=state,
            subtask=state.subtasks["s2"].spec,
            root_artifacts=ArtifactBundle(),
        )
    assert exc.value.reason == DeliveryFailureReason.REQUIRED_SOURCE_NOT_COMMITTED.value


@pytest.mark.asyncio
async def test_d_required_field_missing_blocks_target(tmp_path):
    store = FileArtifactStore(tmp_path)
    plan = _two_subtask_plan(required=True)
    # Source artifact missing required field name used by contract.
    plan = plan.model_copy(
        update={
            "communication_plan": plan.communication_plan.model_copy(
                update={
                    "payload_contracts": [
                        PayloadContract(
                            payload_id="p1",
                            source_subtask_id="s1",
                            target_subtask_id="s2",
                            artifact_type="FinalAnswerArtifact",
                            required=True,
                            required_fields=["missing_field"],
                            max_tokens=2048,
                            metadata={"slot": "comm:p1"},
                        )
                    ]
                }
            )
        }
    )
    state = TaskExecutionState.from_plan(plan)
    state.communication_plan = plan.communication_plan
    source = create_artifact(
        FinalAnswerArtifact(answer="x", source_node="s1"),
        producer_node_id="s1",
        task_id="m5h",
    )
    await store.put(source)
    state.subtasks["s1"].status = SubtaskStatus.COMMITTED
    state.subtasks["s1"].final_output_artifact_id = source.artifact_id
    state.subtasks["s2"].status = SubtaskStatus.READY
    with pytest.raises(CommunicationDeliveryBlocked) as exc:
        await SubtaskInputAssembler(store).assemble(
            task_plan=plan,
            task_state=state,
            subtask=state.subtasks["s2"].spec,
            root_artifacts=ArtifactBundle(),
        )
    assert exc.value.reason == DeliveryFailureReason.REQUIRED_FIELD_MISSING.value


@pytest.mark.asyncio
async def test_e_token_budget_strictness(tmp_path):
    store = FileArtifactStore(tmp_path)
    plan = _two_subtask_plan(required=True)
    plan = plan.model_copy(
        update={
            "communication_plan": plan.communication_plan.model_copy(
                update={
                    "payload_contracts": [
                        PayloadContract(
                            payload_id="p1",
                            source_subtask_id="s1",
                            target_subtask_id="s2",
                            artifact_type="FinalAnswerArtifact",
                            required=True,
                            required_fields=["answer"],
                            max_tokens=40,
                            metadata={"slot": "comm:p1"},
                        )
                    ]
                }
            )
        }
    )
    state = TaskExecutionState.from_plan(plan)
    state.communication_plan = plan.communication_plan
    source = create_artifact(
        FinalAnswerArtifact(answer="x" * 5000, source_node="s1"),
        producer_node_id="s1",
        task_id="m5h",
    )
    await store.put(source)
    state.subtasks["s1"].status = SubtaskStatus.COMMITTED
    state.subtasks["s1"].final_output_artifact_id = source.artifact_id
    state.subtasks["s2"].status = SubtaskStatus.READY
    bundle = await SubtaskInputAssembler(store).assemble(
        task_plan=plan,
        task_state=state,
        subtask=state.subtasks["s2"].spec,
        root_artifacts=ArtifactBundle(),
    )
    # Fits after truncation; ledger records estimated tokens ≤ max_tokens.
    assert "comm:p1" in bundle.slots
    assert state.delivery_ledger[0].estimated_tokens <= 40


@pytest.mark.asyncio
async def test_f_aggregation_list_and_conflict(tmp_path):
    store = FileArtifactStore(tmp_path)
    plan = TaskPlan(
        task_id="m5h",
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
                    metadata={"slot": "shared"},
                ),
                PayloadContract(
                    payload_id="p2",
                    source_subtask_id="s2",
                    target_subtask_id="s3",
                    artifact_type="FinalAnswerArtifact",
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
                    strategy=AggregationStrategy.LIST,
                    target_slot="shared",
                    metadata={"target_subtask_id": "s3", "slot": "shared"},
                )
            ],
            context_budgets={"s3": 50_000},
        ),
    )
    state = TaskExecutionState.from_plan(plan)
    state.communication_plan = plan.communication_plan
    for sid, ans in [("s1", '{"fields":{"v":1}}'), ("s2", '{"fields":{"v":2}}')]:
        art = create_artifact(
            FinalAnswerArtifact(answer=ans, source_node=sid),
            producer_node_id=sid,
            task_id="m5h",
        )
        await store.put(art)
        state.subtasks[sid].status = SubtaskStatus.COMMITTED
        state.subtasks[sid].final_output_artifact_id = art.artifact_id
    state.subtasks["s3"].status = SubtaskStatus.READY
    bundle = await SubtaskInputAssembler(store).assemble(
        task_plan=plan,
        task_state=state,
        subtask=state.subtasks["s3"].spec,
        root_artifacts=ArtifactBundle(),
    )
    assert "shared" in bundle.slots

    # No aggregation rule → fail closed.
    state2 = state.model_copy(deep=True)
    state2.communication_plan = plan.communication_plan.model_copy(
        update={"aggregation_rules": []}
    )
    state2.delivery_ledger = []
    with pytest.raises(CommunicationDeliveryBlocked):
        await SubtaskInputAssembler(store).assemble(
            task_plan=plan,
            task_state=state2,
            subtask=state2.subtasks["s3"].spec,
            root_artifacts=ArtifactBundle(),
        )


def test_g_backend_assignment_materializes_different_hash(tmp_path):
    spec = SubtaskSpec(
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
                "backend_id": "smolagents_code",
                "model_name": "gpt-4o-mini",
            }
        },
    )
    base = FutureGraphMaterializer().materialize(
        subtask=spec.model_copy(update={"metadata": {}}),
        revision_graphs_dir=tmp_path / "base",
        revision_id="base",
    )
    mat = FutureGraphMaterializer().materialize(
        subtask=spec,
        revision_graphs_dir=tmp_path / "graphs",
        revision_id="rev",
        allowed_backend_pools={"default": ["smolagents_code", "codex_sdk"]},
        backend_model_pools={"smolagents_code": ["gpt-4o-mini"]},
    )
    assert mat.graph_hash != base.graph_hash
    agent = next(n for n in mat.graph.nodes if isinstance(n, AgentNodeSpec))
    assert agent.resolved_backend().type == "smolagents_code"


def test_h_model_assignment_changes_model_name(tmp_path):
    spec = SubtaskSpec(
        subtask_id="s3",
        title="s3",
        objective="o",
        dependencies=[],
        keystone_harness_id="repository_test_harness",
        local_graph_template=GRAPH,
        budget=BudgetSpec(),
        metadata={
            "model_assignment": {
                "node_id": "codex_implementer",
                "model_name": "model-b",
            }
        },
    )
    mat = FutureGraphMaterializer().materialize(
        subtask=spec,
        revision_graphs_dir=tmp_path / "graphs",
        revision_id="rev",
    )
    agent = next(n for n in mat.graph.nodes if isinstance(n, AgentNodeSpec))
    assert agent.model is not None
    assert agent.model.name == "model-b"


@pytest.mark.asyncio
async def test_i_plan_revision_atomic_failure_keeps_old_plan(tmp_path):
    plan = _two_subtask_plan()
    state = TaskExecutionState.from_plan(plan)
    state.subtasks["s2"].status = SubtaskStatus.PENDING
    new_plan, new_comm, policy, _ = apply_global_edits(
        task_plan=plan,
        communication_plan=state.communication_plan,
        scheduling_policy=TaskSchedulingPolicy(),
        edits=[ContextBudgetEdit(target_subtask_id="s2", max_tokens=77)],
        eligible_subtask_ids={"s2"},
    )
    rev = GlobalPlanRevision(
        revision_id="rev-atomic-fail",
        revision_number=1,
        parent_plan_hash=plan.content_hash(),
        new_plan_hash=new_plan.content_hash(),
        parent_communication_hash="p",
        new_communication_hash="n",
        edits=[ContextBudgetEdit(target_subtask_id="s2", max_tokens=77)],
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
    )
    old = state.task_plan.content_hash()

    class Boom(TaskCheckpointStore):
        async def save(self, state):  # noqa: ANN001
            raise RuntimeError("checkpoint save failed")

    with pytest.raises(RuntimeError):
        await commit_prepared_revision(
            state=state,
            prepared=prepared,
            checkpoint_store=Boom(tmp_path),
            run_dir=tmp_path,
        )
    assert state.task_plan.content_hash() == old
    assert not (tmp_path / "plan_revisions" / "rev-atomic-fail").exists()


@pytest.mark.asyncio
async def test_j_restart_after_applied_revision(tmp_path):
    state_plan = TaskPlan(
        task_id="m5h",
        plan_version=1,
        decomposition_rationale="r",
        subtasks=[
            SubtaskSpec(
                subtask_id="s1",
                title="s1",
                objective="done",
                dependencies=[],
                keystone_harness_id="h",
                local_graph_template=GRAPH,
                budget=BudgetSpec(),
            ),
            SubtaskSpec(
                subtask_id="s2",
                title="s2",
                objective="run",
                dependencies=["s1"],
                keystone_harness_id="h",
                local_graph_template=GRAPH,
                budget=BudgetSpec(),
            ),
            SubtaskSpec(
                subtask_id="s3",
                title="s3",
                objective="pending",
                dependencies=["s1"],
                keystone_harness_id="h",
                local_graph_template=GRAPH,
                budget=BudgetSpec(),
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
                )
            ],
            context_budgets={"s3": 1000},
        ),
    )
    state = TaskExecutionState.from_plan(state_plan)
    state.communication_plan = state_plan.communication_plan
    state.subtasks["s1"].status = SubtaskStatus.COMMITTED
    state.subtasks["s2"].status = SubtaskStatus.RUNNING
    state.subtasks["s2"].lease_status = "leased"
    state.subtasks["s3"].status = SubtaskStatus.PENDING
    state.committed_subtask_count = 1
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
    rev = state.active_plan_revision_id
    version = state.global_revision
    loaded = await TaskCheckpointStore(tmp_path).load("m5h")
    assert loaded is not None
    assert loaded.active_plan_revision_id == rev
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
    assert len(applied) == 1
    assert loaded.global_revision >= version


def test_k_communication_cycle_rejected():
    from orchestra.communication.validation import (
        CommunicationPlanValidationError,
        validate_communication_plan,
    )

    plan = TaskPlan(
        task_id="t",
        plan_version=1,
        decomposition_rationale="x",
        subtasks=[
            SubtaskSpec(
                subtask_id="s1",
                title="s1",
                objective="s1",
                dependencies=["s2"],
                keystone_harness_id="h",
                local_graph_template=GRAPH,
                budget=BudgetSpec(),
            ),
            SubtaskSpec(
                subtask_id="s2",
                title="s2",
                objective="s2",
                dependencies=[],
                keystone_harness_id="h",
                local_graph_template=GRAPH,
                budget=BudgetSpec(),
            ),
        ],
    )
    with pytest.raises(CommunicationPlanValidationError):
        validate_communication_plan(
            task_plan=plan,
            communication_plan=CommunicationPlan(
                payload_contracts=[
                    PayloadContract(
                        payload_id="p1",
                        source_subtask_id="s1",
                        target_subtask_id="s2",
                        artifact_type="FinalAnswerArtifact",
                        required=True,
                    )
                ]
            ),
        )


def test_l_undeclared_plan_mutation_rejected():
    plan = _two_subtask_plan()
    state = TaskExecutionState.from_plan(plan)
    proposed = plan.model_copy(deep=True)
    new_subs = [
        s.model_copy(update={"objective": "stolen", "keystone_harness_id": "evil"})
        if s.subtask_id == "s2"
        else s
        for s in proposed.subtasks
    ]
    proposed = proposed.model_copy(update={"subtasks": new_subs, "plan_version": 2})
    result = FuturePlanValidator(SlowLoopConfig(enabled=True)).validate(
        current_state=state,
        proposed_plan=proposed,
        edits=[ContextBudgetEdit(target_subtask_id="s2", max_tokens=9)],
        leased_subtask_ids=set(),
    )
    assert result.ok is False
    assert any("UNDECLARED_PLAN_MUTATION" in e for e in result.errors)


def test_m_backend_assignment_edit_roundtrip_no_id_branch():
    # Ensure apply_global_edits + materializer path has no backend-id if/else in controller.
    plan = _two_subtask_plan()
    state = TaskExecutionState.from_plan(plan)
    state.subtasks["s2"].status = SubtaskStatus.PENDING
    new_plan, _, _, _ = apply_global_edits(
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
    meta = new_plan.subtasks[1].metadata["backend_assignment"]
    assert meta["backend_id"] == "smolagents_code"
    mat = FutureGraphMaterializer().materialize(
        subtask=new_plan.subtasks[1],
        allowed_backend_pools={"default": ["smolagents_code", "codex_sdk"]},
        backend_model_pools={"smolagents_code": ["gpt-4o-mini"]},
    )
    agent = next(n for n in mat.graph.nodes if isinstance(n, AgentNodeSpec))
    assert agent.resolved_backend().type == "smolagents_code"
    assert isinstance(agent.model, ModelSpec) or agent.model is not None


def test_n_hidden_artifact_type_rejected():
    from orchestra.communication.compiler import CommunicationPlanCompiler
    from orchestra.communication.validation import CommunicationPlanValidationError

    plan = _two_subtask_plan()
    with pytest.raises(CommunicationPlanValidationError):
        CommunicationPlanCompiler().compile(
            task_plan=plan,
            communication_plan=CommunicationPlan(
                payload_contracts=[
                    PayloadContract(
                        payload_id="priv",
                        source_subtask_id="s1",
                        target_subtask_id="s2",
                        artifact_type="PrivateEvaluatorArtifact",
                    )
                ]
            ),
        )


@pytest.mark.asyncio
async def test_scheduler_delivery_blocked_does_not_run_backend(tmp_path):
    """Required delivery failure is structured and must not look like model failure."""
    store = FileArtifactStore(tmp_path)
    plan = _two_subtask_plan(required=True)
    state = TaskExecutionState.from_plan(plan)
    state.communication_plan = plan.communication_plan
    state.subtasks["s1"].status = SubtaskStatus.PENDING
    state.subtasks["s2"].status = SubtaskStatus.READY
    result = await CommunicationDeliveryEngine(store).deliver_for_target(
        task_plan=plan,
        task_state=state,
        communication_plan=state.communication_plan,
        target_subtask_id="s2",
    )
    assert result.blocked is True
    assert result.block_reason is DeliveryFailureReason.REQUIRED_SOURCE_NOT_COMMITTED
    assert state.subtasks["s2"].status is SubtaskStatus.READY
