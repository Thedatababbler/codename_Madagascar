"""M5 Final Hardening unit tests: delivery rules, ledger, projection, aggregation."""

from __future__ import annotations

import json

import pytest

from orchestra.communication.aggregation import (
    AggregationConflictError,
    AggregationInput,
    AggregationRule,
    AggregationStrategy,
    aggregate_payloads,
)
from orchestra.communication.delivery import CommunicationDeliveryEngine, load_prior_delivery
from orchestra.communication.ledger import (
    DeliveryFailureReason,
    DeliveryRecord,
    DeliveryStatus,
)
from orchestra.communication.payload import (
    DeliveryCondition,
    DeliveryRule,
    DeliveryTrigger,
    PayloadContract,
)
from orchestra.communication.plan import CommunicationPlan
from orchestra.communication.projection import (
    PayloadProjectionInfeasible,
    project_payload,
)
from orchestra.communication.validation import (
    CommunicationPlanValidationError,
    validate_communication_plan,
)
from orchestra.control.slow_loop.edits import apply_global_edits
from orchestra.control.slow_loop.graph_materializer import FutureGraphMaterializer
from orchestra.control.slow_loop.schemas import (
    ContextBudgetEdit,
    SlowLoopConfig,
    TaskSchedulingPolicy,
)
from orchestra.control.slow_loop.validation import FuturePlanValidator
from orchestra.control.task_state import SubtaskStatus, TaskExecutionState
from orchestra.decomposition.schemas import BudgetSpec, SubtaskSpec, TaskPlan
from orchestra.ir.artifacts import create_artifact
from orchestra.ir.nodes import AgentNodeSpec
from orchestra.schemas.artifacts import FinalAnswerArtifact
from orchestra.storage.artifacts import FileArtifactStore


def _task_plan() -> TaskPlan:
    return TaskPlan(
        task_id="t",
        plan_version=1,
        decomposition_rationale="x",
        subtasks=[
            SubtaskSpec(
                subtask_id="s1",
                title="s1",
                objective="s1",
                dependencies=[],
                keystone_harness_id="h",
                local_graph_template="configs/graphs/codex_single_implementer.yaml",
                budget=BudgetSpec(),
            ),
            SubtaskSpec(
                subtask_id="s2",
                title="s2",
                objective="s2",
                dependencies=["s1"],
                keystone_harness_id="h",
                local_graph_template="configs/graphs/codex_single_implementer.yaml",
                budget=BudgetSpec(),
            ),
        ],
    )


@pytest.mark.asyncio
async def test_delivery_requires_enabled_rule(tmp_path):
    store = FileArtifactStore(tmp_path)
    source = create_artifact(
        FinalAnswerArtifact(answer="hello", source_node="s1"),
        producer_node_id="s1",
        task_id="t",
    )
    await store.put(source)
    plan = _task_plan()
    state = TaskExecutionState.from_plan(plan)
    state.subtasks["s1"].status = SubtaskStatus.COMMITTED
    state.subtasks["s1"].final_output_artifact_id = source.artifact_id
    state.subtasks["s2"].status = SubtaskStatus.READY
    contract = PayloadContract(
        payload_id="p1",
        source_subtask_id="s1",
        target_subtask_id="s2",
        artifact_type="FinalAnswerArtifact",
        required=False,
    )
    state.communication_plan = CommunicationPlan(
        version=1,
        payload_contracts=[contract],
        delivery_schedule=[
            DeliveryRule(
                rule_id="r1",
                payload_id="p1",
                enabled=False,
                trigger=DeliveryTrigger.ON_SOURCE_COMMIT,
            )
        ],
        context_budgets={"s2": 10_000},
    )
    engine = CommunicationDeliveryEngine(store)
    result = await engine.deliver_for_target(
        task_plan=plan,
        task_state=state,
        communication_plan=state.communication_plan,
        target_subtask_id="s2",
    )
    assert result.delivered_slots == {}
    assert any(r.status is DeliveryStatus.SKIPPED_NO_RULE for r in result.audit_records)


@pytest.mark.asyncio
async def test_delivery_condition_false_is_audited(tmp_path):
    store = FileArtifactStore(tmp_path)
    source = create_artifact(
        FinalAnswerArtifact(answer="hello", source_node="s1"),
        producer_node_id="s1",
        task_id="t",
    )
    await store.put(source)
    plan = _task_plan()
    state = TaskExecutionState.from_plan(plan)
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
            )
        ],
        delivery_schedule=[
            DeliveryRule(
                rule_id="r1",
                payload_id="p1",
                enabled=True,
                trigger=DeliveryTrigger.ON_SOURCE_COMMIT,
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
    assert result.delivered_slots == {}
    assert any(
        r.status is DeliveryStatus.SKIPPED_CONDITION_FALSE for r in result.audit_records
    )


@pytest.mark.asyncio
async def test_prior_delivery_reloads_projected_artifact(tmp_path):
    store = FileArtifactStore(tmp_path)
    source = create_artifact(
        FinalAnswerArtifact(answer="hello", source_node="s1"),
        producer_node_id="s1",
        task_id="t",
    )
    await store.put(source)
    plan = _task_plan()
    state = TaskExecutionState.from_plan(plan)
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
                metadata={"slot": "comm:p1"},
            )
        ],
        delivery_schedule=[
            DeliveryRule(
                rule_id="r1",
                payload_id="p1",
                trigger=DeliveryTrigger.ON_SOURCE_COMMIT,
            )
        ],
        context_budgets={"s2": 10_000},
    )
    engine = CommunicationDeliveryEngine(store)
    first = await engine.deliver_for_target(
        task_plan=plan,
        task_state=state,
        communication_plan=state.communication_plan,
        target_subtask_id="s2",
    )
    assert first.new_records
    state.delivery_ledger.extend(first.new_records)
    projected_id = first.new_records[0].projected_artifact_id
    second = await engine.deliver_for_target(
        task_plan=plan,
        task_state=state,
        communication_plan=state.communication_plan,
        target_subtask_id="s2",
    )
    assert second.new_records == []
    assert "comm:p1" in second.delivered_slots
    assert second.delivered_slots["comm:p1"].artifact_id == projected_id


@pytest.mark.asyncio
async def test_delivery_ledger_corruption_fails_closed(tmp_path):
    store = FileArtifactStore(tmp_path)
    rec = DeliveryRecord(
        delivery_id="d1",
        communication_plan_version=1,
        rule_id="r1",
        payload_id="p1",
        source_subtask_id="s1",
        target_subtask_id="s2",
        source_artifact_id="a1",
        projected_artifact_id="missing",
        projected_artifact_hash="deadbeef",
        target_slot="comm:p1",
        delivered_at_state_version=1,
        status=DeliveryStatus.DELIVERED,
    )
    with pytest.raises(Exception) as exc:
        await load_prior_delivery(record=rec, artifact_store=store)
    assert "DELIVERY_LEDGER_CORRUPTION" in str(exc.value)


@pytest.mark.asyncio
async def test_required_payload_missing_blocks_target(tmp_path):
    store = FileArtifactStore(tmp_path)
    plan = _task_plan()
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
        delivery_schedule=[
            DeliveryRule(
                rule_id="r1",
                payload_id="p1",
                trigger=DeliveryTrigger.ON_SOURCE_COMMIT,
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
    assert result.block_reason is DeliveryFailureReason.REQUIRED_SOURCE_NOT_COMMITTED


def test_required_field_missing_fails_projection():
    source = create_artifact(
        FinalAnswerArtifact(answer="x", source_node="s1"),
        producer_node_id="s1",
        task_id="t",
    )
    contract = PayloadContract(
        payload_id="p1",
        source_subtask_id="s1",
        target_subtask_id="s2",
        artifact_type="FinalAnswerArtifact",
        required=True,
        required_fields=["missing_field"],
        max_tokens=1000,
    )
    with pytest.raises(PayloadProjectionInfeasible) as exc:
        project_payload(contract=contract, source=source, task_id="t")
    assert exc.value.reason is DeliveryFailureReason.REQUIRED_FIELD_MISSING


def test_recursive_projection_respects_token_limit():
    # Nested content embedded in the answer string / parallel fields via JSON blob.
    source = create_artifact(
        FinalAnswerArtifact(
            answer=json.dumps(
                {
                    "answer": "x" * 2000,
                    "trace": [{"step": "y" * 500} for _ in range(20)],
                    "nested": {"a": ["z" * 300 for _ in range(10)]},
                }
            ),
            source_node="s1",
        ),
        producer_node_id="s1",
        task_id="t",
    )
    contract = PayloadContract(
        payload_id="p1",
        source_subtask_id="s1",
        target_subtask_id="s2",
        artifact_type="FinalAnswerArtifact",
        required_fields=["answer"],
        max_tokens=80,
    )
    result = project_payload(contract=contract, source=source, task_id="t")
    assert result.final_estimated_tokens <= contract.max_tokens


def test_aggregation_list_is_deterministic():
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
    rule = AggregationRule(
        rule_id="agg1",
        source_payload_ids=["p2", "p1"],
        strategy=AggregationStrategy.LIST,
        max_tokens=10_000,
    )
    result = aggregate_payloads(
        rule=rule,
        inputs=AggregationInput(
            source_payload_ids=["p1", "p2"],
            source_artifact_ids=[a1.artifact_id, a2.artifact_id],
            projected_artifacts=[a1, a2],
        ),
        task_id="t",
    )
    # Rule source order: p2 then p1
    import json

    value = json.loads(result.aggregated_artifact.payload["answer"])["value"]
    assert value[0]["v"] == 2
    assert value[1]["v"] == 1


def test_aggregation_dict_conflict_fails_closed():
    a1 = create_artifact(
        FinalAnswerArtifact(answer='{"fields":{"k":"a"}}', source_node="s1"),
        producer_node_id="s1",
        task_id="t",
    )
    a2 = create_artifact(
        FinalAnswerArtifact(answer='{"fields":{"k":"b"}}', source_node="s2"),
        producer_node_id="s2",
        task_id="t",
    )
    with pytest.raises(AggregationConflictError):
        aggregate_payloads(
            rule=AggregationRule(
                rule_id="agg1",
                source_payload_ids=["p1", "p2"],
                strategy=AggregationStrategy.MERGE_DICT_FAIL_ON_CONFLICT,
            ),
            inputs=AggregationInput(
                source_payload_ids=["p1", "p2"],
                source_artifact_ids=[a1.artifact_id, a2.artifact_id],
                projected_artifacts=[a1, a2],
            ),
            task_id="t",
        )


def test_required_communication_cycle_rejected():
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
                local_graph_template="g",
                budget=BudgetSpec(),
            ),
            SubtaskSpec(
                subtask_id="s2",
                title="s2",
                objective="s2",
                dependencies=[],
                keystone_harness_id="h",
                local_graph_template="g",
                budget=BudgetSpec(),
            ),
        ],
    )
    # Fix: s1 depends on s2, required communication s1→s2 forms cycle with deps s2→s1.
    # Actually deps: s2 → s1 (s1 depends on s2), required comm s1 → s2 creates cycle.
    comm = CommunicationPlan(
        payload_contracts=[
            PayloadContract(
                payload_id="p1",
                source_subtask_id="s1",
                target_subtask_id="s2",
                artifact_type="FinalAnswerArtifact",
                required=True,
            )
        ]
    )
    with pytest.raises(CommunicationPlanValidationError):
        validate_communication_plan(task_plan=plan, communication_plan=comm)


def test_future_graph_materializes_backend_assignment(tmp_path):
    spec = SubtaskSpec(
        subtask_id="s3",
        title="s3",
        objective="o",
        dependencies=[],
        keystone_harness_id="repository_test_harness",
        local_graph_template="configs/graphs/codex_single_implementer.yaml",
        budget=BudgetSpec(),
        metadata={
            "backend_assignment": {
                "node_id": "codex_implementer",
                "backend_id": "smolagents_code",
                "model_name": "gpt-4o-mini",
            }
        },
    )
    mat = FutureGraphMaterializer().materialize(
        subtask=spec,
        revision_graphs_dir=tmp_path / "graphs",
        revision_id="rev-1",
        allowed_backend_pools={"default": ["smolagents_code", "codex_sdk"]},
        backend_model_pools={"smolagents_code": ["gpt-4o-mini"]},
    )
    agent = next(n for n in mat.graph.nodes if isinstance(n, AgentNodeSpec))
    assert agent.resolved_backend().type == "smolagents_code"
    assert mat.graph_hash != mat.parent_graph_hash


def test_future_graph_materializes_model_assignment(tmp_path):
    spec = SubtaskSpec(
        subtask_id="s3",
        title="s3",
        objective="o",
        dependencies=[],
        keystone_harness_id="repository_test_harness",
        local_graph_template="configs/graphs/codex_single_implementer.yaml",
        budget=BudgetSpec(),
        metadata={
            "model_assignment": {
                "node_id": "codex_implementer",
                "model_name": "special-model-y",
            }
        },
    )
    mat = FutureGraphMaterializer().materialize(
        subtask=spec,
        revision_graphs_dir=tmp_path / "graphs",
        revision_id="rev-1",
    )
    agent = next(n for n in mat.graph.nodes if isinstance(n, AgentNodeSpec))
    assert agent.model is not None
    assert agent.model.name == "special-model-y"


def test_materialized_graph_snapshot_contains_real_graph(tmp_path):
    spec = SubtaskSpec(
        subtask_id="s3",
        title="s3",
        objective="o",
        dependencies=[],
        keystone_harness_id="repository_test_harness",
        local_graph_template="configs/graphs/codex_single_implementer.yaml",
        budget=BudgetSpec(),
        metadata={
            "backend_assignment": {
                "node_id": "codex_implementer",
                "backend_id": "structured_llm",
            }
        },
    )
    mat = FutureGraphMaterializer().materialize(
        subtask=spec,
        revision_graphs_dir=tmp_path / "graphs",
        revision_id="rev-1",
        allowed_backend_pools={"default": ["structured_llm", "codex_sdk"]},
    )
    text = (tmp_path / "graphs" / "s3.yaml").read_text(encoding="utf-8")
    assert "structured_llm" in text
    assert "graph_hash" in text
    assert mat.execution_config.graph_path.endswith("s3.yaml")


def test_undeclared_plan_delta_rejected():
    plan = _task_plan()
    state = TaskExecutionState.from_plan(plan)
    state.subtasks["s2"].status = SubtaskStatus.PENDING
    proposed = plan.model_copy(deep=True)
    new_subs = []
    for s in proposed.subtasks:
        if s.subtask_id == "s2":
            new_subs.append(
                s.model_copy(
                    update={
                        "objective": "mutated",
                        "dependencies": [],
                    }
                )
            )
        else:
            new_subs.append(s)
    proposed = proposed.model_copy(update={"subtasks": new_subs, "plan_version": 2})
    result = FuturePlanValidator(SlowLoopConfig(enabled=True)).validate(
        current_state=state,
        proposed_plan=proposed,
        edits=[ContextBudgetEdit(target_subtask_id="s2", max_tokens=100)],
        leased_subtask_ids=set(),
    )
    assert result.ok is False
    assert any("UNDECLARED_PLAN_MUTATION" in e for e in result.errors)


@pytest.mark.asyncio
async def test_revision_checkpoint_transaction_rollback(tmp_path):
    from orchestra.control.slow_loop.revision import (
        commit_prepared_revision,
        prepare_revision_staging,
    )
    from orchestra.control.slow_loop.schemas import GlobalPlanRevision, GlobalPlanRevisionStatus
    from orchestra.runtime.task_checkpoint import TaskCheckpointStore

    plan = _task_plan()
    state = TaskExecutionState.from_plan(plan)
    state.subtasks["s2"].status = SubtaskStatus.PENDING
    new_plan, new_comm, policy, _ = apply_global_edits(
        task_plan=plan,
        communication_plan=state.communication_plan,
        scheduling_policy=TaskSchedulingPolicy(),
        edits=[ContextBudgetEdit(target_subtask_id="s2", max_tokens=111)],
        eligible_subtask_ids={"s2"},
    )
    rev = GlobalPlanRevision(
        revision_id="rev-fail-1",
        revision_number=1,
        parent_plan_hash=plan.content_hash(),
        new_plan_hash=new_plan.content_hash(),
        parent_communication_hash="p",
        new_communication_hash="n",
        edits=[ContextBudgetEdit(target_subtask_id="s2", max_tokens=111)],
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
    old_hash = state.task_plan.content_hash()

    class BoomStore(TaskCheckpointStore):
        async def save(self, state):  # noqa: ANN001
            raise RuntimeError("checkpoint boom")

    with pytest.raises(RuntimeError, match="checkpoint boom"):
        await commit_prepared_revision(
            state=state,
            prepared=prepared,
            checkpoint_store=BoomStore(tmp_path),
            run_dir=tmp_path,
        )
    assert state.task_plan.content_hash() == old_hash
    assert state.active_plan_revision_id is None
    # Promote-before-checkpoint: final dir may exist as orphan; must not be active.
    orphan = tmp_path / "plan_revisions" / "rev-fail-1"
    if orphan.exists():
        assert state.active_plan_revision_id != "rev-fail-1"


@pytest.mark.asyncio
async def test_applied_revision_recovers_once(tmp_path):
    from orchestra.control.slow_loop.controller import SlowLoopController
    from orchestra.control.slow_loop.schemas import (
        GlobalPlanRevisionStatus,
        SlowLoopBudget,
        SlowLoopConfig,
    )
    from orchestra.runtime.backend import RunContext
    from orchestra.runtime.limits import RuntimeLimits, RuntimeSemaphores
    from orchestra.runtime.task_checkpoint import TaskCheckpointStore

    plan = _task_plan()
    # Add third pending for context pressure style updates via communication.
    plan = plan.model_copy(
        update={
            "subtasks": list(plan.subtasks)
            + [
                SubtaskSpec(
                    subtask_id="s3",
                    title="s3",
                    objective="s3",
                    dependencies=["s1"],
                    keystone_harness_id="h",
                    local_graph_template="configs/graphs/codex_single_implementer.yaml",
                    budget=BudgetSpec(),
                )
            ],
            "communication_plan": CommunicationPlan(
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
        }
    )
    state = TaskExecutionState.from_plan(plan)
    state.communication_plan = plan.communication_plan
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
    limits = RuntimeLimits(
        max_parallel_benchmark_tasks=1,
        max_parallel_nodes_per_task=2,
        max_parallel_llm_calls=2,
        max_parallel_sandboxes=2,
    )
    ctx = RunContext(
        run_id="r",
        task_id="t",
        run_dir=tmp_path,
        limits=limits,
        semaphores=RuntimeSemaphores(limits),
        contract_hash="c",
    )
    result = await ctrl.maybe_update(
        task_plan=state.task_plan,
        state=state,
        context=ctx,
        leased_subtask_ids={"s2"},
    )
    assert result.updated is True
    rev_id = state.active_plan_revision_id
    store = TaskCheckpointStore(tmp_path)
    loaded = await store.load(state.task_id)
    assert loaded is not None
    assert loaded.active_plan_revision_id == rev_id
    applied = [
        r
        for r in loaded.plan_revision_history
        if r.status is GlobalPlanRevisionStatus.APPLIED
    ]
    assert len(applied) == 1
