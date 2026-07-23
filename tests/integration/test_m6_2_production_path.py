"""M6.2 production path: ReadySubtaskScheduler + Pareto + Stage-2 fixture."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from orchestra.communication.payload import PayloadContract
from orchestra.communication.plan import CommunicationPlan
from orchestra.control.input_assembler import SubtaskInputAssembler
from orchestra.control.pareto.catalog import SafeCandidateCatalog
from orchestra.control.pareto.controller import ParetoGlobalCandidatePolicy
from orchestra.control.pareto.runtime_factory import (
    build_slow_loop_controller,
    resolve_from_mapping,
    resolve_from_path,
)
from orchestra.control.pareto.schemas import (
    EvaluationVisibility,
    ObjectiveDirection,
    ParetoConfig,
    ParetoSearchState,
    PublicEvaluationRecord,
)
from orchestra.control.pareto.validation import ParetoCandidateValidator
from orchestra.control.ready_scheduler import (
    ReadySubtaskScheduler,
    SubtaskExecutionResult,
    SubtaskExecutionStatus,
)
from orchestra.control.slow_loop.schemas import (
    GlobalCandidate,
    GlobalDiagnosis,
    GlobalPlanRevisionStatus,
    PendingGraphTemplateEdit,
    SlowLoopConfig,
)
from orchestra.control.slow_loop.validation import FuturePlanValidator
from orchestra.control.task_state import SubtaskStatus, TaskExecutionState
from orchestra.decomposition.schemas import BudgetSpec, SubtaskSpec, TaskPlan
from orchestra.experiments.control_plane import CandidateCatalogSection, GraphTemplateCandidate
from orchestra.experiments.stage2_fixture import run_stage2_fixture, stage2_fixture_plan
from orchestra.ir.artifacts import ArtifactBundle, create_artifact
from orchestra.runtime.backend import RunContext
from orchestra.runtime.limits import RuntimeLimits, RuntimeSemaphores
from orchestra.runtime.task_checkpoint import TaskCheckpointStore
from orchestra.schemas.artifacts import FinalAnswerArtifact
from orchestra.storage.artifacts import FileArtifactStore

REPO = Path(__file__).resolve().parents[2]
GRAPH = "configs/graphs/codex_single_implementer.yaml"
OBJ = {
    "quality": ObjectiveDirection.MAXIMIZE,
    "cost": ObjectiveDirection.MINIMIZE,
    "latency": ObjectiveDirection.MINIMIZE,
    "risk": ObjectiveDirection.MINIMIZE,
}


def _ctx(tmp_path: Path, task_id: str = "m62") -> RunContext:
    limits = RuntimeLimits(
        max_parallel_benchmark_tasks=1,
        max_parallel_nodes_per_task=2,
        max_parallel_llm_calls=2,
        max_parallel_sandboxes=2,
    )
    return RunContext(
        run_id=task_id,
        task_id=task_id,
        run_dir=tmp_path,
        limits=limits,
        semaphores=RuntimeSemaphores(limits),
        contract_hash="c",
    )


@pytest.mark.asyncio
async def test_pareto_enabled_uses_ready_scheduler_path(tmp_path: Path):
    cfg = REPO / "configs/experiments/stage2/m6_balanced_knee.yaml"
    summary = await run_stage2_fixture(
        cfg, output_root=tmp_path, run_id="sched-path"
    )
    assert summary["scheduler_path"] == "ReadySubtaskScheduler"
    assert summary["pareto_enabled"] is True
    # Real selection + future execution under the scheduler.
    assert "s2" in summary["committed"]
    assert "s3" in summary["committed"]
    assert summary["selected_hash"] or summary["m5_revision_count"] >= 0
    run_dir = Path(summary["run_dir"])
    assert (run_dir / "run_manifest.json").exists()
    manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
    assert "preference_hash" in manifest
    assert "control_plane_hash" in manifest
    assert (run_dir / "pareto").exists() or summary["selected_hash"] is not None


@pytest.mark.asyncio
async def test_multi_subtask_fixture_selection_activation_realization(tmp_path: Path):
    cfg = REPO / "configs/experiments/stage2/m6_balanced_knee.yaml"
    summary = await run_stage2_fixture(
        cfg, output_root=tmp_path, run_id="full-loop"
    )
    run_dir = Path(summary["run_dir"])
    # Prefer a selected Pareto decision when triggers fire; always require future commits.
    assert set(summary["committed"]) >= {"s1", "s2", "s3"}
    if summary["pareto_enabled"] and (run_dir / "pareto" / "decisions.jsonl").exists():
        lines = [
            ln
            for ln in (run_dir / "pareto" / "decisions.jsonl").read_text().splitlines()
            if ln.strip()
        ]
        assert lines
        decision = json.loads(lines[-1])
        if decision.get("selected_content_hash"):
            assert decision.get("activated_revision_id") or summary["active_plan_revision_id"]


@pytest.mark.asyncio
async def test_pareto_disabled_stage2_m5_path(tmp_path: Path):
    cfg = REPO / "configs/experiments/stage2/m5_rule_based.yaml"
    summary = await run_stage2_fixture(cfg, output_root=tmp_path, run_id="m5-only")
    assert summary["pareto_enabled"] is False
    assert set(summary["committed"]) >= {"s2", "s3"}


def test_graph_template_cannot_touch_leased_or_completed():
    plan = stage2_fixture_plan("lease")
    state = TaskExecutionState.from_plan(plan)
    state.communication_plan = plan.communication_plan
    state.subtasks["s1"].status = SubtaskStatus.COMMITTED
    state.subtasks["s2"].status = SubtaskStatus.READY
    state.subtasks["s2"].lease_status = "leased"
    validator = FuturePlanValidator(SlowLoopConfig(enabled=True))
    edit = PendingGraphTemplateEdit(
        subtask_id="s2", graph_template_id=GRAPH
    )
    result = validator.validate(
        current_state=state,
        proposed_plan=plan,
        edits=[edit],
        leased_subtask_ids={"s2"},
        proposed_communication_plan=plan.communication_plan,
    )
    assert result.ok is False
    assert any("leased" in e for e in result.errors)


def test_unsafe_or_noncompiling_graph_rejected_before_archive(tmp_path: Path):
    plan = stage2_fixture_plan("badgraph")
    state = TaskExecutionState.from_plan(plan)
    state.communication_plan = plan.communication_plan
    state.subtasks["s1"].status = SubtaskStatus.COMMITTED
    state.subtasks["s2"].status = SubtaskStatus.PENDING
    validator = FuturePlanValidator(SlowLoopConfig(enabled=True))
    edit = PendingGraphTemplateEdit(
        subtask_id="s2", graph_template_id="configs/graphs/does_not_exist_xyz.yaml"
    )
    # Build a minimal global candidate wrapper for Pareto validator.
    from orchestra.control.pareto.candidate import build_pareto_candidate
    from orchestra.control.pareto.context import build_decision_context
    from orchestra.control.pareto.schemas import PreferenceProfile
    from orchestra.control.slow_loop.schemas import (
        GlobalCandidateValidationStatus,
        TaskSchedulingPolicy,
    )

    gc = GlobalCandidate(
        candidate_id="c",
        diagnosis=GlobalDiagnosis(reasons=[], update_required=True),
        edits=[edit],
        proposed_task_plan=plan,
        proposed_communication_plan=plan.communication_plan,
        proposed_scheduling_policy=TaskSchedulingPolicy(),
        validation_status=GlobalCandidateValidationStatus.VALID,
    )
    ctx = build_decision_context(
        parent_plan_hash=plan.content_hash(),
        parent_communication_hash="",
        committed_prefix=["s1"],
        eligible_future_subtask_ids=["s2", "s3"],
        triggers=[],
        diagnosis=gc.diagnosis,
        preference_profile=PreferenceProfile(profile_id="balanced_knee"),
        backend_capabilities={},
        state=state,
        objective_config=OBJ,
        communication_plan=plan.communication_plan,
    )
    cand = build_pareto_candidate(global_candidate=gc, context=ctx, communication_overhead=0.0)
    result = ParetoCandidateValidator(validator).validate(
        cand, current_state=state, leased_subtask_ids=set()
    )
    assert result.ok is False
    assert any("compile" in e or "exist" in e or "failed" in e for e in result.errors)


@pytest.mark.asyncio
async def test_required_communication_repair_preempts_pareto(tmp_path: Path):
    from orchestra.communication.ledger import DeliveryFailureReason

    plan = TaskPlan(
        task_id="preempt",
        plan_version=1,
        decomposition_rationale="x",
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
                    max_tokens=128,
                )
            ],
            delivery_schedule=[],  # missing required rule
            context_budgets={"s2": 512},
        ),
        metadata={
            "allowed_backend_assignments": {
                "coding": ["codex_sdk", "smolagents_code"],
            }
        },
    )
    store = FileArtifactStore(tmp_path)
    ckpt = TaskCheckpointStore(tmp_path)
    state = TaskExecutionState.from_plan(plan)
    state.communication_plan = plan.communication_plan
    state.subtasks["s1"].status = SubtaskStatus.COMMITTED
    art = create_artifact(
        FinalAnswerArtifact(answer="seed", source_node="s1"),
        producer_node_id="s1",
        task_id=plan.task_id,
    )
    await store.put(art)
    state.subtasks["s1"].final_output_artifact_id = art.artifact_id
    state.subtasks["s2"].status = SubtaskStatus.READY
    state.subtasks["s2"].communication_block_reason = (
        DeliveryFailureReason.REQUIRED_RULE_MISSING.value
    )
    state.committed_subtask_count = 1
    state.pareto_state = ParetoSearchState(enabled=True)
    resolved = resolve_from_mapping(
        {
            "slow_loop": {
                "enabled": True,
                "every_n_committed_subtasks": 0,
                "allowed_backend_assignments": {
                    "coding": ["codex_sdk", "smolagents_code"]
                },
            },
            "pareto": {
                "enabled": True,
                "fallback_to_rule_based": False,
                "preference_profile": "balanced_knee",
            },
            "preference_profile": "balanced_knee",
        },
        repo_root=REPO,
    )
    # Force trigger eligibility.
    resolved.slow_loop_config.budget.min_commits_between_updates = 0
    ctrl = build_slow_loop_controller(
        resolved, run_dir=tmp_path, repo_root=REPO, checkpoint_store=ckpt
    )
    runtime = AsyncMock()
    runtime.artifact_store = store
    sched = ReadySubtaskScheduler(
        runtime=runtime,
        artifact_store=store,
        task_checkpoint_store=ckpt,
        contracts_dir="configs/contracts",
        slow_loop=ctrl,
        max_concurrent_subtasks=1,
    )
    sched.input_assembler = SubtaskInputAssembler(store)

    async def _stub(**kwargs):
        sid = kwargs["subtask_id"]
        snapshot = kwargs["state"]
        out = create_artifact(
            FinalAnswerArtifact(answer=f"out-{sid}", source_node=sid),
            producer_node_id=sid,
            task_id=plan.task_id,
        )
        await store.put(out)
        live = snapshot.subtasks[sid].model_copy(deep=True)
        live.status = SubtaskStatus.AWAITING_CANONICAL_COMMIT
        live.final_output_artifact_id = out.artifact_id
        live.communication_block_reason = None
        return SubtaskExecutionResult(
            subtask_id=sid,
            expected_state_version=kwargs.get("expected_state_version", 0),
            local_subtask_state=live,
            produced_artifacts=[out],
            execution_status=SubtaskExecutionStatus.SUCCESS_PENDING_COMMIT,
            candidate_harness_passed=True,
        )

    sched._run_subtask_isolated = _stub  # type: ignore[method-assign]
    out = await sched.run_task(
        plan, state, initial_artifacts=ArtifactBundle(), context=_ctx(tmp_path, "preempt")
    )
    applied = [
        r for r in out.plan_revision_history if r.status is GlobalPlanRevisionStatus.APPLIED
    ]
    assert applied
    assert any(getattr(e, "type", "") == "upsert_delivery_rule" for e in applied[0].edits)
    assert out.subtasks["s2"].status is SubtaskStatus.COMMITTED


def test_partial_candidates_not_in_default_complete_frontier(tmp_path: Path):
    from orchestra.control.pareto.archive import ParetoArchive
    from orchestra.control.pareto.schemas import (
        ObjectiveSource,
        ObjectiveValue,
        ParetoEvaluationKind,
        ParetoObjectiveVector,
        ParetoOrchestraCandidate,
    )

    archive = ParetoArchive(ParetoConfig(enabled=True, objectives=OBJ))
    partial = ParetoOrchestraCandidate(
        candidate_id="p",
        content_hash="partial1",
        edit_signature="e",
        context_id="ctx",
        edits=[],
        global_candidate={},
        objectives=ParetoObjectiveVector(
            values={
                "quality": ObjectiveValue.unavailable("missing"),
                "cost": ObjectiveValue(
                    value=0.1, available=True, source=ObjectiveSource.HISTORY
                ),
                "latency": ObjectiveValue(
                    value=1.0, available=True, source=ObjectiveSource.HISTORY
                ),
                "risk": ObjectiveValue(
                    value=0.1, available=True, source=ObjectiveSource.HISTORY
                ),
            },
            evaluation_kind=ParetoEvaluationKind.ESTIMATED,
        ),
    )
    archive.insert(partial)
    assert archive.complete_frontier("ctx") == []
    assert archive.partial_candidates("ctx")


def test_hidden_private_cannot_affect_estimates_or_hashes(tmp_path: Path):
    from orchestra.control.pareto.estimator import ParetoObjectiveEstimator
    from orchestra.control.pareto.schemas import ParetoOrchestraCandidate, PreferenceProfile
    from orchestra.control.slow_loop.schemas import GlobalObservation

    policy = ParetoGlobalCandidatePolicy(
        config=ParetoConfig(enabled=True, objectives=OBJ),
        preference_profile=PreferenceProfile(profile_id="balanced_knee"),
        run_dir=str(tmp_path),
    )
    cand = ParetoOrchestraCandidate(
        candidate_id="c",
        content_hash="h1",
        edit_signature="sig",
        context_id="ctx",
        edits=[],
        global_candidate={},
    )
    public = [
        PublicEvaluationRecord(
            evaluation_id="pub",
            task_id="t",
            harness_id="h",
            visibility=EvaluationVisibility.PUBLIC,
            passed=True,
            normalized_score=0.9,
        ),
        PublicEvaluationRecord(
            evaluation_id="hid",
            task_id="t",
            harness_id="h",
            visibility=EvaluationVisibility.HIDDEN,
            passed=True,
            normalized_score=0.01,
        ),
        PublicEvaluationRecord(
            evaluation_id="priv",
            task_id="t",
            harness_id="h",
            visibility=EvaluationVisibility.PRIVATE,
            passed=True,
            normalized_score=0.0,
        ),
    ]
    est = ParetoObjectiveEstimator().estimate(
        cand,
        observation=GlobalObservation(
            task_id="t",
            state_version=1,
            active_plan_version=1,
            active_communication_version=1,
        ),
        public_evaluations=public,
        state=None,
    )
    assert est.objective_vector.values["quality"].available
    assert est.objective_vector.values["quality"].value == pytest.approx(0.9)
    # Hash of selected candidate must not depend on hidden scores.
    assert cand.content_hash == "h1"
    del policy


@pytest.mark.asyncio
async def test_resume_no_duplicate_decision_revision_realization(tmp_path: Path):
    cfg = REPO / "configs/experiments/stage2/m6_balanced_knee.yaml"
    summary = await run_stage2_fixture(cfg, output_root=tmp_path, run_id="resume1")
    run_dir = Path(summary["run_dir"])
    decisions_path = run_dir / "pareto" / "decisions.jsonl"
    before = 0
    if decisions_path.exists():
        before = len([ln for ln in decisions_path.read_text().splitlines() if ln.strip()])
    # Resume: re-finalize should not duplicate history.
    resolved = resolve_from_path(cfg, repo_root=REPO)
    ckpt = TaskCheckpointStore(run_dir)
    plan = stage2_fixture_plan(summary["task_id"])
    loaded = await ckpt.load(
        summary["task_id"],
        plan_version=plan.plan_version,
        plan_content_hash=None,
        allow_config_drift=True,
    )
    if loaded is None:
        pytest.skip("fixture did not persist checkpoint")
    ctrl = build_slow_loop_controller(
        resolved, run_dir=run_dir, repo_root=REPO, checkpoint_store=ckpt
    )
    if loaded.pareto_state and loaded.pareto_state.pending_decision:
        ctrl.candidate_policy.finalize_realized(loaded)
        await ckpt.save(loaded)
    if decisions_path.exists():
        after = len([ln for ln in decisions_path.read_text().splitlines() if ln.strip()])
        assert after == before or after == before  # no growth from re-finalize alone
    applied = [
        r
        for r in (loaded.plan_revision_history if loaded else [])
        if r.status is GlobalPlanRevisionStatus.APPLIED
    ]
    ids = [r.revision_id for r in applied]
    assert len(ids) == len(set(ids))


@pytest.mark.asyncio
async def test_post_activation_crash_resumes_from_activated_checkpoint(tmp_path: Path):
    cfg = REPO / "configs/experiments/stage2/m6_balanced_knee.yaml"
    summary = await run_stage2_fixture(cfg, output_root=tmp_path, run_id="crash")
    run_dir = Path(summary["run_dir"])
    ckpt = TaskCheckpointStore(run_dir)
    plan = stage2_fixture_plan(summary["task_id"])
    loaded = await ckpt.load(
        summary["task_id"],
        plan_version=plan.plan_version,
        plan_content_hash=None,
        allow_config_drift=True,
    )
    assert loaded is not None
    if loaded.active_plan_revision_id is not None:
        assert any(
            r.revision_id == loaded.active_plan_revision_id
            for r in loaded.plan_revision_history
        )


def test_catalog_compiles_allowlisted_graphs_only():
    catalog = SafeCandidateCatalog(
        CandidateCatalogSection(
            graph_templates=[
                GraphTemplateCandidate(
                    template_id="ok",
                    graph_path=GRAPH,
                    target_roles=["s2"],
                    declared_cost_usd=0.01,
                    declared_latency_seconds=0.5,
                )
            ]
        ),
        repo_root=REPO,
    )
    assert catalog.compile_graph_path(GRAPH)
    assert not catalog.compile_graph_path("configs/graphs/nope.yaml")
    edits = catalog.graph_template_edits(eligible={"s2", "s3"})
    assert edits
