"""M6 integration: telemetry bridge, frontier, preference, M5 transaction."""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestra.communication.payload import DeliveryRule, PayloadContract
from orchestra.communication.plan import CommunicationPlan
from orchestra.control.pareto.archive import ParetoArchive
from orchestra.control.pareto.controller import ParetoGlobalCandidatePolicy
from orchestra.control.pareto.dominance import dominates
from orchestra.control.pareto.schemas import (
    ObjectiveDirection,
    ObjectiveSource,
    ObjectiveValue,
    ParetoConfig,
    ParetoEvaluationKind,
    ParetoObjectiveVector,
    ParetoOrchestraCandidate,
    PreferenceProfile,
)
from orchestra.control.pareto.selector import DeterministicParetoSelector
from orchestra.control.slow_loop.controller import SlowLoopController
from orchestra.control.slow_loop.schemas import SlowLoopBudget, SlowLoopConfig
from orchestra.control.task_state import SubtaskStatus, TaskExecutionState
from orchestra.decomposition.schemas import BudgetSpec, SubtaskSpec, TaskPlan
from orchestra.ir.artifacts import create_artifact
from orchestra.ir.graph import load_graph
from orchestra.llm.usage import LLMUsage
from orchestra.runtime.backend import RunContext
from orchestra.runtime.committer import WaveCommitter
from orchestra.runtime.limits import RuntimeLimits, RuntimeSemaphores
from orchestra.runtime.scheduler import Scheduler
from orchestra.runtime.state import NodeExecutionResult, NodeStatus, RuntimeState
from orchestra.runtime.task_checkpoint import TaskCheckpointStore
from orchestra.schemas.artifacts import FinalAnswerArtifact
from orchestra.storage.artifacts import FileArtifactStore

GRAPH = "configs/graphs/codex_single_implementer.yaml"
OBJ = {
    "quality": ObjectiveDirection.MAXIMIZE,
    "cost": ObjectiveDirection.MINIMIZE,
    "latency": ObjectiveDirection.MINIMIZE,
    "risk": ObjectiveDirection.MINIMIZE,
}


def _ctx(tmp_path: Path) -> RunContext:
    limits = RuntimeLimits(
        max_parallel_benchmark_tasks=1,
        max_parallel_nodes_per_task=2,
        max_parallel_llm_calls=2,
        max_parallel_sandboxes=2,
    )
    return RunContext(
        run_id="m6",
        task_id="m6",
        run_dir=tmp_path,
        limits=limits,
        semaphores=RuntimeSemaphores(limits),
        contract_hash="c",
    )


def _plan() -> TaskPlan:
    return TaskPlan(
        task_id="m6",
        plan_version=1,
        decomposition_rationale="m6",
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
                    required=False,
                    max_tokens=9000,
                    metadata={"slot": "comm:p12"},
                ),
                PayloadContract(
                    payload_id="p23",
                    source_subtask_id="s2",
                    target_subtask_id="s3",
                    artifact_type="FinalAnswerArtifact",
                    required=False,
                    max_tokens=9000,
                    metadata={"slot": "comm:p23"},
                ),
            ],
            delivery_schedule=[
                DeliveryRule(rule_id="r12", payload_id="p12", enabled=True),
                DeliveryRule(rule_id="r23", payload_id="p23", enabled=True),
            ],
            context_budgets={"s2": 1200, "s3": 1200},
        ),
        metadata={
            "allowed_backend_assignments": {
                "s2": ["codex_sdk", "smolagents_code"],
                "s3": ["codex_sdk", "smolagents_code"],
            }
        },
    )


def _vec(**kwargs) -> ParetoObjectiveVector:
    return ParetoObjectiveVector(
        values={
            k: ObjectiveValue(value=float(v), source=ObjectiveSource.ESTIMATED, available=True)
            for k, v in kwargs.items()
        },
        evaluation_kind=ParetoEvaluationKind.ESTIMATED,
    )


def _cand(cid: str, **kwargs) -> ParetoOrchestraCandidate:
    return ParetoOrchestraCandidate(
        candidate_id=cid,
        content_hash=cid,
        edit_signature=cid,
        context_id="ctx",
        global_candidate=None,
        objectives=_vec(**kwargs),
    )


@pytest.mark.asyncio
async def test_telemetry_through_wave_committer(tmp_path: Path):
    store = FileArtifactStore(tmp_path)
    graph = load_graph(GRAPH)
    node_id = graph.nodes[0].node_id
    art = create_artifact(
        FinalAnswerArtifact(answer="ok", source_node=node_id),
        producer_node_id=node_id,
        task_id="m6",
    )
    result = NodeExecutionResult(
        node_id=node_id,
        succeeded=True,
        outputs={graph.final_output_slot: art},
        latency_ms=77,
        usage=LLMUsage(prompt_tokens=33, completion_tokens=9, cached_tokens=1),
        backend_id="fake_backend",
        backend_metadata={"model_name": "fake-test-model", "session_ref": {
            "backend_id": "fake_backend",
            "session_id": "sess-m6",
        }},
    )
    state = RuntimeState(
        run_id="r",
        task_id="m6",
        graph_id=graph.graph_id,
        graph_hash=graph.content_hash,
        contract_hash="c",
        node_status={n.node_id: NodeStatus.RUNNING for n in graph.nodes},
    )
    new_state, *_ = await WaveCommitter(store, Scheduler(max_parallel_nodes=2)).commit_wave(
        graph=graph, previous_state=state, results=[result]
    )
    snap = new_state.node_usage_snapshots[node_id]
    assert snap.prompt_tokens == 33
    assert snap.completion_tokens == 9
    assert snap.latency_ms == 77
    assert snap.backend_id == "fake_backend"


def test_estimated_frontier_excludes_dominated():
    archive = ParetoArchive(
        ParetoConfig(
            objectives={
                "quality": ObjectiveDirection.MAXIMIZE,
                "cost": ObjectiveDirection.MINIMIZE,
                "latency": ObjectiveDirection.MINIMIZE,
                "risk": ObjectiveDirection.MINIMIZE,
            }
        )
    )
    a = _cand("A", quality=1.0, cost=5.0, latency=2.0, risk=0.1)
    b = _cand("B", quality=0.6, cost=1.0, latency=4.0, risk=0.2)
    c = _cand("C", quality=0.5, cost=6.0, latency=3.0, risk=0.3)  # dominated by A
    d = _cand("D", quality=0.7, cost=3.0, latency=0.5, risk=0.8)
    for cand in (a, b, c, d):
        archive.insert(cand)
    frontier = {x.content_hash for x in archive.complete_frontier("ctx")}
    assert "C" not in frontier
    assert {"A", "B", "D"} <= frontier
    assert dominates(a, c, OBJ)


def test_preference_selection_profiles():
    frontier = [
        _cand("A", quality=1.0, cost=5.0, latency=2.0, risk=0.1),
        _cand("B", quality=0.6, cost=1.0, latency=4.0, risk=0.2),
        _cand("D", quality=0.7, cost=3.0, latency=0.5, risk=0.8),
    ]
    sel = DeterministicParetoSelector()
    assert (
        sel.select(frontier, PreferenceProfile(profile_id="quality_first"), OBJ).content_hash
        == "A"
    )
    assert (
        sel.select(
            frontier,
            PreferenceProfile(profile_id="cost_capped_quality", maximum_cost_usd=2.0),
            OBJ,
        ).content_hash
        == "B"
    )
    assert (
        sel.select(
            frontier,
            PreferenceProfile(profile_id="latency_capped_quality", maximum_latency_seconds=1.0),
            OBJ,
        ).content_hash
        == "D"
    )
    knee = sel.select(frontier, PreferenceProfile(profile_id="balanced_knee"), OBJ)
    assert knee is not None


@pytest.mark.asyncio
async def test_selected_candidate_uses_m5_transaction(tmp_path: Path):
    from datetime import UTC, datetime

    from orchestra.control.backend_usage import BackendUsageRecord
    from orchestra.control.pareto.schemas import (
        EvaluationVisibility,
        ParetoSearchState,
        PublicEvaluationRecord,
    )

    plan = _plan()
    state = TaskExecutionState.from_plan(plan)
    state.communication_plan = plan.communication_plan
    state.subtasks["s1"].status = SubtaskStatus.COMMITTED
    state.subtasks["s2"].status = SubtaskStatus.PENDING
    state.subtasks["s3"].status = SubtaskStatus.PENDING
    state.committed_subtask_count = 1
    state.pareto_state = ParetoSearchState(enabled=True)
    state.public_evaluation_records = [
        PublicEvaluationRecord(
            evaluation_id="e1",
            task_id="m6",
            harness_id="repository_test_harness",
            visibility=EvaluationVisibility.PUBLIC,
            passed=True,
            normalized_score=1.0,
        )
    ]
    now = datetime.now(UTC)
    state.backend_usage_records = [
        BackendUsageRecord(
            usage_id="u1",
            task_id="m6",
            subtask_id="s1",
            node_id="n",
            backend_id="codex_sdk",
            attempt_id=1,
            started_at=now,
            finished_at=now,
            latency_seconds=0.4,
            prompt_tokens=10,
            completion_tokens=5,
            estimated_cost_usd=0.01,
            cost_quality="exact",
            accounting_source="hist",
            status="success",
            model_name="fake-test-model",
        )
    ]
    policy = ParetoGlobalCandidatePolicy(
        config=ParetoConfig(enabled=True, max_candidates=8, objectives=OBJ),
        preference_profile=PreferenceProfile(profile_id="balanced_knee"),
        run_dir=str(tmp_path),
    )
    ctrl = SlowLoopController(
        config=SlowLoopConfig(
            enabled=True,
            budget=SlowLoopBudget(
                context_pressure_ratio=0.5,
                min_commits_between_updates=1,
                max_candidates_per_update=8,
            ),
            allowed_backend_assignments={
                "coding": ["codex_sdk", "smolagents_code"],
            },
        ),
        candidate_policy=policy,
        checkpoint_store=TaskCheckpointStore(tmp_path),
    )
    result = await ctrl.maybe_update(
        task_plan=plan,
        state=state,
        context=_ctx(tmp_path),
        leased_subtask_ids=set(),
    )
    assert result.message in {
        "applied",
        "NO_SAFE_FUTURE_EDIT",
        "NO_COMPARABLE_PARETO_CANDIDATE",
        "no trigger",
        "diagnosis NO_CHANGE",
    }
    if result.updated:
        assert state.active_plan_revision_id is not None
        assert state.pareto_state is not None
        assert state.pareto_state.pending_decision is not None
        assert (
            state.pareto_state.pending_decision.activated_revision_id
            == state.active_plan_revision_id
        )


@pytest.mark.asyncio
async def test_hidden_evaluator_isolation_rejects_private_contracts(tmp_path: Path):
    from orchestra.control.pareto.validation import ParetoCandidateValidator
    from orchestra.control.slow_loop.schemas import GlobalCandidate, GlobalDiagnosis

    plan = _plan()
    state = TaskExecutionState.from_plan(plan)
    bad_comm = plan.communication_plan.model_copy(deep=True)
    bad_comm.payload_contracts[0] = bad_comm.payload_contracts[0].model_copy(
        update={"artifact_type": "PrivateArtifact"}
    )
    gc = GlobalCandidate(
        candidate_id="bad",
        diagnosis=GlobalDiagnosis(),
        edits=[],
        proposed_task_plan=plan,
        proposed_communication_plan=bad_comm,
        proposed_scheduling_policy=state.scheduling_policy
        or __import__(
            "orchestra.control.slow_loop.schemas", fromlist=["TaskSchedulingPolicy"]
        ).TaskSchedulingPolicy(),
    )
    cand = ParetoOrchestraCandidate(
        candidate_id="bad",
        content_hash="bad",
        edit_signature="bad",
        context_id="ctx",
        global_candidate=gc,
    )
    result = ParetoCandidateValidator().validate(
        cand, current_state=state, leased_subtask_ids=set()
    )
    assert result.ok is False
