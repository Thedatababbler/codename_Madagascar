"""M6.2.1 — calibration fail-closed, canonical evidence, ownership, realization."""

from __future__ import annotations

import hashlib
import json
import multiprocessing as mp
import os
import shutil
from pathlib import Path

import pytest
import yaml

from orchestra.cli.stage2_pareto_experiments import (
    cmd_freeze_calibration,
    cmd_report,
)
from orchestra.control.run_ownership import RunOwnership, RunOwnershipError
from orchestra.control.scheduler_recovery import begin_scheduler_session
from orchestra.control.task_state import SubtaskStatus, TaskExecutionState
from orchestra.experiments.control_plane import load_control_plane_mapping
from orchestra.experiments.private_labels import (
    load_private_labels,
    private_label_access_log,
    reset_private_label_access_log,
)
from orchestra.experiments.stage2_fixture import run_stage2_fixture, stage2_fixture_plan
from orchestra.experiments.stage2_pareto import (
    CalibrationArtifact,
    CalibrationFreezeError,
    CalibrationMismatchError,
    _development_observation_rows,
    assert_calibration_matches,
    collect_run_records,
    write_calibration_artifact,
    write_stage2_report,
)

REPO = Path(__file__).resolve().parents[2]
CFG = REPO / "configs/experiments/stage2/m6_balanced_knee.yaml"
LABEL_A = REPO / "tests/fixtures/private_labels_a.json"
LABEL_B = REPO / "tests/fixtures/private_labels_b.json"

# Selection-relevant frozen fields checked against control-plane recomputation.
# Source/run provenance (source_run_id, source_manifest_hash, config_hash,
# dataset_split_identity) is persisted separately and must not contaminate
# selection_config_hash; those fields are covered by held-out identity tests.
FROZEN_FIELDS = [
    "schema_version",
    "source_split",
    "git_sha",
    "selection_config_hash",
    "preference_hash",
    "objective_hash",
    "control_plane_hash",
    "pricing_version",
    "pricing_registry_hash",
    "candidate_catalog_hash",
    "graph_catalog_hash",
    "resolved_graph_hash",
    "benchmark_manifest_hash",
    "backend_kinds",
    "model_identifiers",
    "backend_model_settings",
    "seed_policy",
    "private_data_policy",
    "public_evaluator_id",
    "public_evaluator_version",
    "preference_profile_id",
]


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _as_dev_run(src: Path, dst: Path) -> Path:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    manifest = json.loads((dst / "run_manifest.json").read_text(encoding="utf-8"))
    manifest["split"] = "development"
    manifest["private_data_policy"] = "private_labels_offline_only"
    manifest["public_evaluator_id"] = "public_harness"
    manifest["public_evaluator_version"] = "public-harness-v1"
    (dst / "run_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return dst


def _freeze_from_dev(dev: Path, output: Path) -> dict:
    control = load_control_plane_mapping(
        yaml.safe_load(CFG.read_text(encoding="utf-8")) or {}
    )
    points, provenance = _development_observation_rows(dev)
    art = write_calibration_artifact(
        output,
        control=control,
        development_points=points,
        run_dir=dev,
        config_path=CFG,
        source_record_ids=provenance,
    )
    return art.to_dict()


def _canonical_online_projection(run_dir: Path) -> dict:
    """Project online control-plane records, excluding run-scoped IDs/paths."""
    rec = collect_run_records(run_dir)
    decisions = sorted(
        [
            {
                "selected_content_hash": d.get("selected_content_hash"),
                "selection_status": d.get("selection_status"),
                "realization_status": d.get("realization_status"),
                "evaluation_kind": d.get("evaluation_kind"),
                "edit_signature": (
                    (d.get("selected_candidate_snapshot") or {}).get("edit_signature")
                ),
            }
            for d in rec["decisions"]
            if str(d.get("evaluation_kind") or "") != "oracle"
        ],
        key=lambda r: str(r.get("selected_content_hash") or ""),
    )
    traces = sorted(
        [
            {
                "candidate_content_hash": t.get("candidate_content_hash"),
                "feasibility_status": t.get("feasibility_status"),
                "frontier_member": t.get("frontier_member"),
                "dominated": t.get("dominated"),
            }
            for t in rec["traces"]
        ],
        key=lambda r: str(r.get("candidate_content_hash") or ""),
    )
    payload = {
        "decisions": decisions,
        "traces": traces,
        "selected_hashes": [d["selected_content_hash"] for d in decisions],
        "fork_effective_concurrency": (rec.get("summary") or {}).get(
            "fork_wave_effective_concurrency"
        ),
    }
    blob = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return {
        "payload": payload,
        "hash": hashlib.sha256(blob.encode()).hexdigest(),
    }


@pytest.mark.asyncio
async def test_freeze_rejects_fixture_and_heldout_copies_split(tmp_path: Path):
    summary = await run_stage2_fixture(CFG, output_root=tmp_path, run_id="fx-split")
    fixture_dir = Path(summary["run_dir"])

    class NS:
        config = str(CFG)
        run_dir = [str(fixture_dir)]
        output = str(tmp_path / "bad.json")

    with pytest.raises(SystemExit, match="development"):
        cmd_freeze_calibration(NS())

    held = tmp_path / "held"
    held.mkdir()
    (held / "run_manifest.json").write_text(
        json.dumps({"split": "heldout", "run_id": "h"}), encoding="utf-8"
    )

    class NS2:
        config = str(CFG)
        run_dir = [str(held)]
        output = str(tmp_path / "bad2.json")

    with pytest.raises(SystemExit, match="development"):
        cmd_freeze_calibration(NS2())

    dev = _as_dev_run(fixture_dir, tmp_path / "dev-ok")
    payload = _freeze_from_dev(dev, tmp_path / "ok.json")
    assert payload["source_split"] == "development"
    assert payload["source_split"] == json.loads(
        (dev / "run_manifest.json").read_text(encoding="utf-8")
    )["split"]
    assert "fallback_default" not in json.dumps(payload)
    assert payload["normalization_source_record_ids"]


@pytest.mark.asyncio
async def test_missing_development_evidence_no_default_normalization(tmp_path: Path):
    run = tmp_path / "empty-dev"
    run.mkdir()
    (run / "run_manifest.json").write_text(
        json.dumps(
            {
                "split": "development",
                "run_id": "empty",
                "started_at": "2020-01-01T00:00:00+00:00",
                "private_data_policy": "private_labels_offline_only",
                "public_evaluator_id": "public_harness",
                "public_evaluator_version": "public-harness-v1",
            }
        ),
        encoding="utf-8",
    )
    (run / "pareto").mkdir()
    (run / "pareto" / "decisions.jsonl").write_text("", encoding="utf-8")
    control = load_control_plane_mapping(
        yaml.safe_load(CFG.read_text(encoding="utf-8")) or {}
    )
    with pytest.raises(CalibrationFreezeError, match="missing development evidence"):
        write_calibration_artifact(
            tmp_path / "cal.json",
            control=control,
            development_points=[],
            run_dir=run,
            config_path=CFG,
            source_record_ids={},
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("field", FROZEN_FIELDS)
async def test_every_frozen_field_mutation_rejected(tmp_path: Path, field: str):
    summary = await run_stage2_fixture(CFG, output_root=tmp_path, run_id=f"mut-{field[:12]}")
    dev = _as_dev_run(Path(summary["run_dir"]), tmp_path / f"dev-{field[:12]}")
    payload = _freeze_from_dev(dev, tmp_path / f"cal-{field[:12]}.json")
    control = load_control_plane_mapping(
        yaml.safe_load(CFG.read_text(encoding="utf-8")) or {}
    )
    mutated = dict(payload)
    if field in {"backend_kinds"}:
        mutated[field] = ["mutated_backend"]
    elif field in {
        "model_identifiers",
        "backend_model_settings",
        "seed_policy",
        "dataset_split_identity",
    }:
        mutated[field] = {"mutated": True}
    elif field == "source_split":
        mutated[field] = "heldout"
        mutated["split"] = "heldout"
    else:
        mutated[field] = f"mutated-{field}"
    cal = CalibrationArtifact.from_dict(mutated)
    with pytest.raises((CalibrationMismatchError, RuntimeError)):
        assert_calibration_matches(cal, control)


@pytest.mark.asyncio
async def test_heldout_matching_accepted_and_wrong_targets_rejected(tmp_path: Path):
    summary = await run_stage2_fixture(CFG, output_root=tmp_path, run_id="match-src")
    dev = _as_dev_run(Path(summary["run_dir"]), tmp_path / "match-dev")
    cal_path = tmp_path / "cal.json"
    payload = _freeze_from_dev(dev, cal_path)
    control = load_control_plane_mapping(
        yaml.safe_load(CFG.read_text(encoding="utf-8")) or {}
    )

    held = tmp_path / "held-ok"
    held.mkdir()
    held_manifest = {
        "split": "heldout",
        "run_id": "held-1",
        "started_at": "2020-01-01T00:00:00+00:00",
        "schema_version": payload["schema_version"],
        "git_sha": payload["git_sha"],
        "git_commit": payload["git_sha"],
        "selection_config_hash": payload["selection_config_hash"],
        "control_plane_hash": payload["control_plane_hash"],
        "preference_hash": payload["preference_hash"],
        "objective_hash": payload["objective_hash"],
        "objectives": payload["objectives"],
        "objective_directions": payload["objective_directions"],
        "objective_required": payload["objective_required"],
        "preference_profile_id": payload["preference_profile_id"],
        "preference_profile": payload["preference_profile"],
        "pricing_version": payload["pricing_version"],
        "pricing_registry_hash": payload["pricing_registry_hash"],
        "candidate_catalog_hash": payload["candidate_catalog_hash"],
        "graph_catalog_hash": payload["graph_catalog_hash"],
        "resolved_graph_hash": payload["resolved_graph_hash"],
        "benchmark_manifest_hash": payload["benchmark_manifest_hash"],
        "manifest_hash": payload["benchmark_manifest_hash"],
        "private_data_policy": payload["private_data_policy"],
        "public_evaluator_id": payload["public_evaluator_id"],
        "public_evaluator_version": payload["public_evaluator_version"],
        "backend_kinds": payload["backend_kinds"],
        "model_identifiers": payload["model_identifiers"],
        "backend_model_settings": payload["backend_model_settings"],
        "seed_policy": payload["seed_policy"],
        "dataset_identity": dict(payload.get("dataset_identity") or {}),
        "dataset_split_identity": {"split": "heldout", "run_id": "held-1"},
        "stage2": {"mode": "m6_balanced_knee", "seed": 42},
    }
    (held / "run_manifest.json").write_text(
        json.dumps(held_manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (held / "pareto").mkdir()
    (held / "pareto" / "decisions.jsonl").write_text("", encoding="utf-8")
    (held / "stage2_fixture_summary.json").write_text(
        json.dumps({"mode": "m6_balanced_knee", "committed": [], "task_id": "held"}),
        encoding="utf-8",
    )
    cal = CalibrationArtifact.from_dict(payload)
    assert_calibration_matches(
        cal, control, require_held_out_split=True, run_manifest=held_manifest
    )

    class NS:
        run_dir = [str(held)]
        output_dir = str(tmp_path / "held-report")
        calibration = str(cal_path)
        config = str(CFG)
        held_out = True
        include_oracle = False
        seed = 42

    assert cmd_report(NS()) == 0

    # development target rejected
    class NSDev:
        run_dir = [str(dev)]
        output_dir = str(tmp_path / "dev-report")
        calibration = str(cal_path)
        config = str(CFG)
        held_out = True
        include_oracle = False
        seed = 42

    with pytest.raises(SystemExit, match="held-out"):
        cmd_report(NSDev())


@pytest.mark.asyncio
async def test_fixture_and_production_reports_read_complete_usage(tmp_path: Path):
    summary = await run_stage2_fixture(CFG, output_root=tmp_path, run_id="rep-fx")
    run_dir = Path(summary["run_dir"])
    assert summary["restart_recovery_counts"] == 0
    assert "hist-codex" in summary["usage_ids"]
    assert "hist-smol" in summary["usage_ids"]
    assert any(u.startswith("wave-s") for u in summary["usage_ids"])
    assert summary["usage_record_count"] == 6
    assert summary["total_cost_usd"] == pytest.approx(0.078)
    assert summary["cost_per_solved"] == pytest.approx(0.078)
    assert summary["solved_task_count"] == 1

    rec = collect_run_records(run_dir)
    assert set(rec["usage_ids"]) == set(summary["usage_ids"])
    assert len(rec["usage_records"]) == 6

    a = tmp_path / "r1"
    b = tmp_path / "r2"
    write_stage2_report([run_dir], output_dir=a, seed=42)
    write_stage2_report([run_dir], output_dir=b, seed=42)
    names = [
        "stage2_main_results.csv",
        "stage2_estimated_vs_realized.csv",
        "stage2_summary.json",
        "stage2_summary.md",
        "pareto_quality_cost.svg",
        "pareto_quality_latency.svg",
    ]
    hashes = {}
    for name in names:
        assert (a / name).read_bytes() == (b / name).read_bytes()
        hashes[name] = _file_hash(a / name)
    main = (a / "stage2_main_results.csv").read_text(encoding="utf-8")
    assert "0.078" in main
    assert ",0," in main or "restart_recovery_counts" in (
        a / "stage2_summary.json"
    ).read_text(encoding="utf-8")
    est = (a / "stage2_estimated_vs_realized.csv").read_text(encoding="utf-8")
    assert "decision_id" in est
    assert "wave_id" in est
    assert "estimated_provenance" in est or "estimator_provenance" in est


@pytest.mark.asyncio
async def test_usage_merged_exactly_once_after_resume(tmp_path: Path):
    with pytest.raises(RuntimeError, match="FAILPOINT:after_future_wave_started"):
        await run_stage2_fixture(
            CFG,
            output_root=tmp_path,
            run_id="merge-once",
            failpoint="after_future_wave_started",
        )
    summary = await run_stage2_fixture(
        CFG, output_root=tmp_path, run_id="merge-once", failpoint=None
    )
    assert summary["restart_recovery_counts"] == 1
    assert len(summary["recovery_ids"]) == 1
    ids = summary["usage_ids"]
    assert len(ids) == len(set(ids))
    assert summary["usage_record_count"] == len(ids)
    # Historical + four wave usages retained exactly once.
    assert "hist-codex" in ids and "hist-smol" in ids
    assert sum(1 for u in ids if u.startswith("wave-")) == 4


@pytest.mark.asyncio
async def test_clean_run_zero_recovery_failpoints_one(tmp_path: Path):
    clean = await run_stage2_fixture(CFG, output_root=tmp_path, run_id="clean0")
    assert clean["restart_recovery_counts"] == 0
    assert clean["recovery_ids"] == []

    for fp, rid in (
        ("after_activation_checkpoint", "fp-act"),
        ("after_future_wave_started", "fp-wave"),
    ):
        with pytest.raises(RuntimeError, match=fp):
            await run_stage2_fixture(
                CFG, output_root=tmp_path, run_id=rid, failpoint=fp
            )
        summary = await run_stage2_fixture(
            CFG, output_root=tmp_path, run_id=rid, failpoint=None
        )
        assert summary["restart_recovery_counts"] == 1, fp
        assert len(summary["recovery_ids"]) == 1, fp

    with pytest.raises(RuntimeError, match="after_realization_persisted"):
        await run_stage2_fixture(
            CFG,
            output_root=tmp_path,
            run_id="fp-real",
            failpoint="after_realization_persisted",
        )
    done = await run_stage2_fixture(
        CFG, output_root=tmp_path, run_id="fp-real", failpoint=None
    )
    assert done["restart_recovery_counts"] == 0


@pytest.mark.asyncio
async def test_wave_binding_and_attempt_execution_revision(tmp_path: Path):
    with pytest.raises(RuntimeError, match="FAILPOINT:after_future_wave_started"):
        await run_stage2_fixture(
            CFG,
            output_root=tmp_path,
            run_id="bind-wave",
            failpoint="after_future_wave_started",
        )
    run_dir = tmp_path / "bind-wave"
    ckpt_files = list((run_dir / "tasks").rglob("task_execution.json"))
    assert ckpt_files
    ckpt = json.loads(ckpt_files[0].read_text(encoding="utf-8"))
    pending = (ckpt.get("pareto_state") or {}).get("pending_decision")
    assert pending is not None
    assert (pending.get("realization_status") or "pending") == "pending"
    assert pending.get("affected_wave_id")
    wave_id = pending["affected_wave_id"]
    waves = ckpt.get("scheduler_wave_records") or []
    bound = next(w for w in waves if w.get("wave_id") == wave_id)
    assert bound.get("terminal_state") != "terminal"
    assert bound.get("plan_revision") == pending.get("activated_revision_id")

    summary = await run_stage2_fixture(
        CFG, output_root=tmp_path, run_id="bind-wave", failpoint=None
    )
    assert summary["restart_recovery_counts"] == 1
    assert summary["pending_decision"] is False
    decisions = [
        json.loads(line)
        for line in (run_dir / "pareto" / "decisions.jsonl").read_text().splitlines()
        if line.strip()
    ]
    realized = [d for d in decisions if d.get("realization_status") == "realized"]
    assert len(realized) == 1
    assert realized[0]["decision_id"] == pending["decision_id"]
    assert realized[0]["activated_revision_id"] == pending["activated_revision_id"]
    assert realized[0]["affected_wave_id"]
    # Same binding or explicit recovery continuation rebound.
    evidence = realized[0].get("realization_evidence") or {}
    assert realized[0]["affected_wave_id"] == wave_id or evidence.get(
        "previous_affected_wave_id"
    ) == wave_id or evidence.get("binding_status") in {
        "bound",
        "rebound_recovery_continuation",
        "realized",
    }
    assert realized[0]["realization_id"]

    # Reload after resume
    ckpt_files = list((run_dir / "tasks").rglob("task_execution.json"))
    final = json.loads(ckpt_files[0].read_text(encoding="utf-8"))
    for sid in ("s2", "s3"):
        attempts = (final.get("subtasks") or {}).get(sid, {}).get("attempts") or []
        assert attempts
        last = attempts[-1]
        assert last.get("execution_plan_revision") == realized[0]["activated_revision_id"]
        assert last.get("wave_id")


@pytest.mark.asyncio
async def test_post_realization_resume_no_duplicates(tmp_path: Path):
    with pytest.raises(RuntimeError, match="FAILPOINT:after_realization_persisted"):
        await run_stage2_fixture(
            CFG,
            output_root=tmp_path,
            run_id="no-dup",
            failpoint="after_realization_persisted",
        )
    run_dir = tmp_path / "no-dup"
    before_dec = [
        json.loads(line)
        for line in (run_dir / "pareto" / "decisions.jsonl").read_text().splitlines()
        if line.strip()
    ]
    summary = await run_stage2_fixture(
        CFG, output_root=tmp_path, run_id="no-dup", failpoint=None
    )
    after_dec = [
        json.loads(line)
        for line in (run_dir / "pareto" / "decisions.jsonl").read_text().splitlines()
        if line.strip()
    ]
    assert len(after_dec) == len(before_dec)
    ids = [d["decision_id"] for d in after_dec]
    assert len(ids) == len(set(ids))
    realized = [d for d in after_dec if d.get("realization_status") == "realized"]
    assert len(realized) == 1
    assert len(summary["usage_ids"]) == len(set(summary["usage_ids"]))


def _owner_hold(run_dir: str, ready_q, release_q) -> None:
    ownership = RunOwnership(run_dir, owner_id="proc-a")
    ownership.acquire()
    ready_q.put({"pid": os.getpid(), "owner": ownership.read_owner_record()})
    release_q.get()
    ownership.release()


def test_live_owner_blocks_second_scheduler_process(tmp_path: Path):
    run_dir = tmp_path / "owned"
    run_dir.mkdir()
    ready_q: mp.Queue = mp.Queue()
    release_q: mp.Queue = mp.Queue()
    proc = mp.Process(target=_owner_hold, args=(str(run_dir), ready_q, release_q))
    proc.start()
    info = ready_q.get(timeout=10)
    assert info["pid"] == proc.pid
    with pytest.raises(RunOwnershipError):
        RunOwnership(run_dir, owner_id="proc-b").acquire()
    release_q.put("release")
    proc.join(timeout=10)
    assert proc.exitcode == 0
    # After A terminates, C can acquire and reclaim.
    with RunOwnership(run_dir, owner_id="proc-c"):
        plan = stage2_fixture_plan("own")
        state = TaskExecutionState.from_plan(plan)
        state.scheduler_incarnation = 1
        state.subtasks["s2"].status = SubtaskStatus.READY
        state.subtasks["s2"].lease_status = "leased"
        state.subtasks["s2"].lease_id = "stale-from-a"
        state.subtasks["s2"].lease_owner_incarnation = 1
        event = begin_scheduler_session(state, reason="dead_owner_resume")
        assert event is not None
        assert event["recovery_id"]
        assert "stale-from-a" in event["reclaimed_lease_ids"]
        assert state.subtasks["s2"].lease_status == "unleased"


@pytest.mark.asyncio
async def test_private_label_mutation_leaves_online_control_unchanged(tmp_path: Path):
    reset_private_label_access_log()
    assert LABEL_A.read_text() != LABEL_B.read_text()
    labels_a = load_private_labels(LABEL_A, purpose="test_setup", allow_online=False)
    labels_b = load_private_labels(LABEL_B, purpose="test_setup", allow_online=False)
    assert labels_a["tasks"]["stage2-fixture"]["private_score"] != labels_b["tasks"][
        "stage2-fixture"
    ]["private_score"]
    reset_private_label_access_log()

    summary_a = await run_stage2_fixture(
        CFG,
        output_root=tmp_path,
        run_id="priv-a",
        private_labels_path=LABEL_A,
    )
    summary_b = await run_stage2_fixture(
        CFG,
        output_root=tmp_path,
        run_id="priv-b",
        private_labels_path=LABEL_B,
    )
    assert summary_a["offline_private_score"] != summary_b["offline_private_score"]
    assert summary_a["selected_hash"] == summary_b["selected_hash"]
    assert summary_a["fork_wave_effective_concurrency"] == summary_b[
        "fork_wave_effective_concurrency"
    ]
    proj_a = _canonical_online_projection(Path(summary_a["run_dir"]))
    proj_b = _canonical_online_projection(Path(summary_b["run_dir"]))
    assert proj_a["hash"] == proj_b["hash"]
    # Online purposes must never appear in the access log.
    online = {
        e
        for e in private_label_access_log()
        if e.get("purpose")
        in {
            "selector",
            "estimator",
            "feasibility",
            "activation",
            "scheduler",
            "online_control",
            "candidate_generation",
        }
    }
    assert online == set()
    # Offline post-execution reads are expected once per run.
    offline = [
        e
        for e in private_label_access_log()
        if e.get("purpose") == "offline_post_execution_report"
    ]
    assert len(offline) == 2


@pytest.mark.asyncio
async def test_candidate_counts_from_persisted_traces(tmp_path: Path):
    summary = await run_stage2_fixture(CFG, output_root=tmp_path, run_id="cand-pop")
    rec = collect_run_records(Path(summary["run_dir"]))
    assert rec["traces"]
    generated = sum(int(t.get("generated_count") or 0) for t in rec["traces"])
    rejected = sum(int(t.get("rejected_count") or 0) for t in rec["traces"])
    out = tmp_path / "rep"
    write_stage2_report([Path(summary["run_dir"])], output_dir=out, seed=42)
    main = (out / "stage2_main_results.csv").read_text(encoding="utf-8")
    assert str(generated) in main or generated == 0
    assert "generated_candidates" in main
    del rejected
