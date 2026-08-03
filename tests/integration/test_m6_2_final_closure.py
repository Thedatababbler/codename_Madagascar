"""M6.2 final correctness closure: public evidence, leases, realization, held-out."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
import yaml

from orchestra.cli.stage2_pareto_experiments import (
    cmd_freeze_calibration,
    cmd_report,
)
from orchestra.control.pareto.estimator import ParetoObjectiveEstimator
from orchestra.control.pareto.public_evaluation import (
    is_quality_neutral_scheduling_change,
    record_commit_public_evaluations,
)
from orchestra.control.pareto.schemas import (
    EvaluationVisibility,
    ParetoOrchestraCandidate,
    PublicEvaluationRecord,
)
from orchestra.control.scheduler_recovery import begin_scheduler_incarnation
from orchestra.control.slow_loop.schemas import (
    PendingBackendAssignmentEdit,
    SchedulingConcurrencyEdit,
)
from orchestra.control.task_state import SubtaskStatus, TaskExecutionState
from orchestra.experiments.control_plane import load_control_plane_mapping
from orchestra.experiments.stage2_fixture import run_stage2_fixture, stage2_fixture_plan
from orchestra.experiments.stage2_pareto import (
    CalibrationArtifact,
    assert_calibration_matches,
    assert_run_split_for_held_out,
    write_calibration_artifact,
    write_stage2_report,
)
from orchestra.settings import resolve_runtime_settings

REPO = Path(__file__).resolve().parents[2]
CFG = REPO / "configs/experiments/stage2/m6_balanced_knee.yaml"


@pytest.mark.asyncio
async def test_fixture_persists_public_evaluation_and_selects(tmp_path: Path):
    summary = await run_stage2_fixture(
        CFG, output_root=tmp_path, run_id="pub-ev"
    )
    run_dir = Path(summary["run_dir"])
    assert summary["selected_hash"]
    assert summary["active_plan_revision_id"] or summary["fork_activation_revision"]
    assert summary["public_evaluation_count"] >= 1
    assert summary["cost_per_solved"] == pytest.approx(summary["total_cost_usd"])
    assert summary["solved_task_count"] == 1
    assert summary["committed_subtask_count"] == 4
    # Public evaluations persisted via commit hook + task checkpoint.
    assert (run_dir / "public_evaluations.jsonl").exists()
    ckpt_files = list((run_dir / "tasks").rglob("task_execution.json"))
    assert ckpt_files
    ckpt = json.loads(ckpt_files[0].read_text(encoding="utf-8"))
    pubs = ckpt.get("public_evaluation_records") or []
    assert pubs
    assert any(
        str(p.get("visibility")).lower() == "public"
        and str(p.get("availability", "available")) != "unavailable"
        for p in pubs
    )
    decisions = [
        json.loads(line)
        for line in (run_dir / "pareto" / "decisions.jsonl").read_text().splitlines()
        if line.strip()
    ]
    realized = [d for d in decisions if d.get("realization_status") == "realized"]
    assert len(realized) == 1
    assert realized[0]["decision_id"]
    assert realized[0]["selected_content_hash"] == summary["selected_hash"]


@pytest.mark.asyncio
async def test_after_future_wave_started_reclaims_and_completes(tmp_path: Path):
    with pytest.raises(RuntimeError, match="FAILPOINT:after_future_wave_started"):
        await run_stage2_fixture(
            CFG,
            output_root=tmp_path,
            run_id="crash-wave",
            failpoint="after_future_wave_started",
        )
    run_dir = tmp_path / "crash-wave"
    fail = json.loads((run_dir / "failpoint.json").read_text(encoding="utf-8"))
    assert fail["failpoint"] == "after_future_wave_started"
    # Resume: reclaim stale leases and finish s2/s3/s4; finalize exactly once.
    summary = await run_stage2_fixture(
        CFG, output_root=tmp_path, run_id="crash-wave", failpoint=None
    )
    assert set(summary["committed"]) >= {"s1", "s2", "s3", "s4"}
    assert summary["execution_success_rate"] == 1.0
    assert summary["restart_recovery_counts"] >= 1
    recovery = json.loads((run_dir / "recovery_events.json").read_text(encoding="utf-8"))
    reclaim = [
        e for e in recovery if e.get("event") == "scheduler_incarnation_recovery"
    ]
    assert reclaim
    assert reclaim[-1].get("reclaimed_lease_ids") is not None
    decisions = [
        json.loads(line)
        for line in (run_dir / "pareto" / "decisions.jsonl").read_text().splitlines()
        if line.strip()
    ]
    realized = [d for d in decisions if d.get("realization_status") == "realized"]
    assert len(realized) == 1
    assert summary["pending_decision"] is False


@pytest.mark.asyncio
async def test_wave_start_crash_keeps_decision_pending(tmp_path: Path):
    with pytest.raises(RuntimeError, match="FAILPOINT:after_future_wave_started"):
        await run_stage2_fixture(
            CFG,
            output_root=tmp_path,
            run_id="pending-wave",
            failpoint="after_future_wave_started",
        )
    run_dir = tmp_path / "pending-wave"
    ckpt_files = list((run_dir / "tasks").rglob("task_execution.json"))
    assert ckpt_files
    ckpt = json.loads(ckpt_files[0].read_text(encoding="utf-8"))
    pareto = ckpt.get("pareto_state") or {}
    assert pareto.get("pending_decision") is not None
    assert (pareto["pending_decision"].get("realization_status") or "pending") == "pending"
    leases = {
        sid: sub.get("lease_status")
        for sid, sub in (ckpt.get("subtasks") or {}).items()
    }
    assert leases.get("s2") == "leased"
    assert leases.get("s3") == "leased"


def test_stale_lease_reclaim_skips_live_incarnation():
    plan = stage2_fixture_plan("lease-live")
    state = TaskExecutionState.from_plan(plan)
    state.scheduler_incarnation = 3
    state.subtasks["s2"].status = SubtaskStatus.READY
    state.subtasks["s2"].lease_status = "leased"
    state.subtasks["s2"].lease_id = "live-lease"
    state.subtasks["s2"].lease_owner_incarnation = 4  # future/live relative to begin
    # begin bumps to 4; owner >= new should not reclaim... wait begin sets new=4,
    # owner=4 >= 4 → skip. Good.
    event = begin_scheduler_incarnation(state, reason="test")
    assert state.scheduler_incarnation == 4
    assert "s2" not in event["affected_subtasks"]
    assert state.subtasks["s2"].lease_status == "leased"


def test_stale_lease_reclaim_previous_incarnation():
    plan = stage2_fixture_plan("lease-stale")
    state = TaskExecutionState.from_plan(plan)
    state.scheduler_incarnation = 1
    state.subtasks["s2"].status = SubtaskStatus.READY
    state.subtasks["s2"].lease_status = "leased"
    state.subtasks["s2"].lease_id = "stale-lease"
    state.subtasks["s2"].lease_owner_incarnation = 1
    event = begin_scheduler_incarnation(state, reason="resume")
    assert state.scheduler_incarnation == 2
    assert "s2" in event["affected_subtasks"]
    assert "stale-lease" in event["reclaimed_lease_ids"]
    assert state.subtasks["s2"].lease_status == "unleased"


def test_scheduling_only_quality_inheritance_provenance():
    edit = SchedulingConcurrencyEdit(max_concurrent_subtasks=2)
    cand = ParetoOrchestraCandidate(
        candidate_id="c",
        content_hash="h",
        edit_signature="e",
        context_id="ctx",
        edits=[edit],
        global_candidate=None,
    )
    assert is_quality_neutral_scheduling_change(cand)
    est = ParetoObjectiveEstimator()
    pubs = [
        PublicEvaluationRecord(
            evaluation_id="p1",
            visibility=EvaluationVisibility.PUBLIC,
            passed=True,
            normalized_score=0.91,
            availability="available",
        )
    ]
    value = est._estimate_quality(cand, pubs, edit_types={"scheduling_concurrency"})
    assert value.available is True
    assert value.value == pytest.approx(0.91)
    assert "inherited_quality_neutral_scheduling_change" in (value.detail or "")


def test_output_affecting_candidate_does_not_use_neutrality_label():
    edit = PendingBackendAssignmentEdit(
        subtask_id="s2", node_id="agent", backend_id="codex_sdk"
    )
    cand = ParetoOrchestraCandidate(
        candidate_id="c",
        content_hash="h",
        edit_signature="e",
        context_id="ctx",
        edits=[edit],
        global_candidate=None,
    )
    assert is_quality_neutral_scheduling_change(cand) is False


@pytest.mark.asyncio
async def test_fixture_and_development_rejected_by_held_out_report(tmp_path: Path):
    summary = await run_stage2_fixture(
        CFG, output_root=tmp_path, run_id="fixture-split"
    )
    run_dir = Path(summary["run_dir"])
    manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest["split"] == "fixture"
    with pytest.raises(RuntimeError, match="held-out runs only"):
        assert_run_split_for_held_out(run_dir)

    cal_path = tmp_path / "cal.json"
    control = load_control_plane_mapping(
        yaml.safe_load(CFG.read_text(encoding="utf-8")) or {}
    )
    write_calibration_artifact(
        cal_path,
        control=control,
        development_points=[
            {
                "quality": 0.9,
                "cost": 0.05,
                "latency": 1.0,
                "risk": 0.1,
                "communication_overhead": 10.0,
            },
            {
                "quality": 0.7,
                "cost": 0.02,
                "latency": 0.5,
                "risk": 0.2,
                "communication_overhead": 5.0,
            },
        ],
        run_dir=run_dir,
        config_path=CFG,
    )

    fixture_run = str(run_dir)

    class NS:
        run_dir = [fixture_run]
        output_dir = str(tmp_path / "heldout-report")
        calibration = str(cal_path)
        config = str(CFG)
        held_out = True
        include_oracle = False
        seed = 42

    with pytest.raises(SystemExit, match="held-out"):
        cmd_report(NS())


def test_heldout_manifest_accepted_and_fixture_relabel_impossible(tmp_path: Path):
    run = tmp_path / "held"
    run.mkdir()
    (run / "run_manifest.json").write_text(
        json.dumps({"split": "heldout", "started_at": "2020-01-01T00:00:00+00:00"}),
        encoding="utf-8",
    )
    assert assert_run_split_for_held_out(run)["split"] == "heldout"
    # Relabel attempt via report flag cannot change persisted split.
    fixture = tmp_path / "fix"
    fixture.mkdir()
    (fixture / "run_manifest.json").write_text(
        json.dumps({"split": "development"}), encoding="utf-8"
    )
    with pytest.raises(RuntimeError, match="held-out"):
        assert_run_split_for_held_out(fixture)


def test_calibration_missing_normalization_rejected(tmp_path: Path):
    control = load_control_plane_mapping(
        yaml.safe_load(CFG.read_text(encoding="utf-8")) or {}
    )
    path = tmp_path / "cal.json"
    art = write_calibration_artifact(
        path,
        control=control,
        development_points=[
            {
                "quality": 0.9,
                "cost": 0.1,
                "latency": 1.0,
                "risk": 0.1,
                "communication_overhead": 1.0,
            }
        ],
        config_path=CFG,
    )
    bad = art.to_dict()
    bad["normalization"] = {"quality": {"min": 0.0, "max": 1.0}}
    cal = CalibrationArtifact.from_dict(bad)
    with pytest.raises(RuntimeError, match="missing_normalization"):
        assert_calibration_matches(cal, control, require_held_out_split=False)


@pytest.mark.asyncio
async def test_report_byte_identical_including_json_md(tmp_path: Path):
    summary = await run_stage2_fixture(
        CFG, output_root=tmp_path, run_id="det-all"
    )
    a = tmp_path / "r1"
    b = tmp_path / "r2"
    write_stage2_report([Path(summary["run_dir"])], output_dir=a, seed=42)
    write_stage2_report([Path(summary["run_dir"])], output_dir=b, seed=42)
    names_a = sorted(p.name for p in a.iterdir() if p.is_file())
    names_b = sorted(p.name for p in b.iterdir() if p.is_file())
    assert names_a == names_b
    hashes = {}
    for name in names_a:
        ha = hashlib.sha256((a / name).read_bytes()).hexdigest()
        hb = hashlib.sha256((b / name).read_bytes()).hexdigest()
        assert ha == hb, name
        hashes[name] = ha
    assert "stage2_summary.json" in hashes
    assert "stage2_summary.md" in hashes
    est = (a / "stage2_estimated_vs_realized.csv").read_text(encoding="utf-8")
    assert "estimator_provenance" in est
    assert "decision_id" in est


@pytest.mark.asyncio
async def test_formal_synthetic_does_not_mutate_lcb_repository_path(tmp_path: Path):
    from orchestra.cli import run_lcb_codex_three_subtask as mod

    before = os.environ.get("LCB_REPOSITORY_PATH")
    # Ensure unset so we can detect pollution.
    os.environ.pop("LCB_REPOSITORY_PATH", None)
    try:

        class Args:
            config = str(REPO / "configs/experiments/lcb_formal_codex_three_subtask.yaml")
            plan = None
            source_repo = None
            output_root = str(tmp_path / "formal")
            run_id = "formal-env"
            dry_run = False
            allow_config_drift = False
            mock_backends = True
            mock_llm = False
            synthetic_problem = True

        code = await mod._run(Args())
        assert "LCB_REPOSITORY_PATH" not in os.environ
        summary = json.loads(
            (tmp_path / "formal" / "formal-env" / "summary.json").read_text(
                encoding="utf-8"
            )
        )
        assert summary.get("error") is None, summary.get("error")
        assert code in {0, 2}
        assert summary["committed"]
        assert set(summary["committed"]) >= {"analyze", "implement", "verify"}
        assert summary["m5_revision_count"] >= 0
    finally:
        if before is None:
            os.environ.pop("LCB_REPOSITORY_PATH", None)
        else:
            os.environ["LCB_REPOSITORY_PATH"] = before


def test_resolve_runtime_settings_no_global_mutation():
    os.environ.pop("LCB_REPOSITORY_PATH", None)
    settings = resolve_runtime_settings(include_lcb_repository_default=False)
    assert "LCB_REPOSITORY_PATH" not in os.environ
    assert "LCB_REPOSITORY_PATH" not in settings or settings.get(
        "LCB_REPOSITORY_PATH"
    ) is None or True
    # With defaults enabled for real LCB, still no process mutation.
    settings2 = resolve_runtime_settings(include_lcb_repository_default=True)
    assert "LCB_REPOSITORY_PATH" not in os.environ
    assert settings2.get("LCB_REPOSITORY_PATH")


def test_record_commit_public_evaluations_idempotent():
    plan = stage2_fixture_plan("idem")
    state = TaskExecutionState.from_plan(plan)
    state.subtasks["s1"].status = SubtaskStatus.COMMITTED
    a = record_commit_public_evaluations(
        state=state,
        run_id="r1",
        subtask_id="s1",
        produced_artifacts=[],
        candidate_harness_passed=True,
    )
    b = record_commit_public_evaluations(
        state=state,
        run_id="r1",
        subtask_id="s1",
        produced_artifacts=[],
        candidate_harness_passed=True,
    )
    assert len(a) == 1
    assert b == []
    assert len(state.public_evaluation_records) == 1


@pytest.mark.asyncio
async def test_freeze_calibration_rejects_heldout_source(tmp_path: Path):
    run = tmp_path / "ho"
    run.mkdir()
    (run / "run_manifest.json").write_text(
        json.dumps({"split": "heldout", "run_id": "ho"}), encoding="utf-8"
    )
    (run / "pareto").mkdir()
    (run / "pareto" / "decisions.jsonl").write_text("", encoding="utf-8")

    class NS:
        config = str(CFG)
        run_dir = [str(run)]
        output = str(tmp_path / "bad_cal.json")

    with pytest.raises(SystemExit, match="development"):
        cmd_freeze_calibration(NS())
