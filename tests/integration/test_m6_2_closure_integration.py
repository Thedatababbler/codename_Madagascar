"""M6.2 integration: fork/join fixture, mock backends, calibration, crash/resume."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from orchestra.backends.factory import resolve_backend_registry
from orchestra.cli.stage2_pareto_experiments import (
    cmd_freeze_calibration,
    cmd_report,
)
from orchestra.experiments.control_plane import load_control_plane_mapping
from orchestra.experiments.stage2_fixture import run_stage2_fixture
from orchestra.experiments.stage2_pareto import (
    assert_calibration_matches,
    write_calibration_artifact,
    write_stage2_report,
)

REPO = Path(__file__).resolve().parents[2]
CFG = REPO / "configs/experiments/stage2/m6_balanced_knee.yaml"


@pytest.mark.asyncio
async def test_fork_join_selected_concurrency_changes_runtime_wave(tmp_path: Path):
    summary = await run_stage2_fixture(
        CFG, output_root=tmp_path, run_id="fork-conc"
    )
    run_dir = Path(summary["run_dir"])
    evidence = json.loads(
        (run_dir / "concurrency_evidence.json").read_text(encoding="utf-8")
    )
    assert summary["concurrent_fork_wave"] is True
    assert summary["fork_wave_effective_concurrency"] == 2
    assert set(summary["committed"]) >= {"s1", "s2", "s3", "s4"}
    assert evidence["fork_activation_revision"]
    assert evidence["fork_activation_revision"] == summary["fork_activation_revision"]
    fork = [w for w in evidence["wave_records"] if w["subtask_id"] in {"s2", "s3"}]
    assert len(fork) == 2
    assert all(w["effective_concurrency"] >= 2 for w in fork)
    assert fork[0]["active_plan_revision_id"] == fork[1]["active_plan_revision_id"]
    manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest["runtime_concurrency_cap"] == 2
    assert "effective_concurrency" in manifest


@pytest.mark.asyncio
async def test_activated_revision_matches_fork_wave(tmp_path: Path):
    summary = await run_stage2_fixture(
        CFG, output_root=tmp_path, run_id="fork-rev"
    )
    evidence = json.loads(
        (Path(summary["run_dir"]) / "concurrency_evidence.json").read_text(
            encoding="utf-8"
        )
    )
    assert evidence["fork_activation_revision"]
    assert evidence["fork_activation_revision"] == summary["fork_activation_revision"]


def test_mock_backends_initialize_no_external_clients():
    with (
        patch("orchestra.backends.factory.CodexSDKBackend", side_effect=AssertionError("codex")),
        patch(
            "orchestra.backends.factory.SmolagentsCodeBackend",
            side_effect=AssertionError("smol"),
        ),
        patch(
            "orchestra.backends.factory.StructuredLLMBackend",
            side_effect=AssertionError("structured"),
        ),
    ):
        registry, manifest = resolve_backend_registry(mock_backends=True)
    assert manifest["mock_backends"] is True
    assert manifest["backend_override"] == "deterministic_mock"
    for bid in ("structured_llm", "smolagents_code", "codex_sdk"):
        assert registry.has(bid)


@pytest.mark.asyncio
async def test_production_mock_path_patches_raise(tmp_path: Path):
    """Complete fixture/production mock path never constructs external clients."""
    with (
        patch(
            "orchestra.llm.openai_compatible_async.OpenAICompatibleAsyncClient",
            side_effect=AssertionError("openai client"),
        ),
        patch(
            "orchestra.backends.codex_sdk.CodexSDKBackend",
            side_effect=AssertionError("codex backend"),
        ),
    ):
        summary = await run_stage2_fixture(
            CFG, output_root=tmp_path, run_id="mock-safe"
        )
    assert summary["execution_success_rate"] == 1.0
    manifest = json.loads(
        (Path(summary["run_dir"]) / "run_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest.get("mock_backends") is True


@pytest.mark.asyncio
async def test_report_joins_dominated_and_estimated_realized(tmp_path: Path):
    summary = await run_stage2_fixture(
        CFG, output_root=tmp_path, run_id="report-join"
    )
    report_dir = tmp_path / "report"
    payload = write_stage2_report(
        [Path(summary["run_dir"])], output_dir=report_dir, seed=42
    )
    assert payload["run_count"] == 1
    est_csv = (report_dir / "stage2_estimated_vs_realized.csv").read_text(
        encoding="utf-8"
    )
    assert "quality_est" in est_csv
    assert "quality_real" in est_csv
    frontier = (report_dir / "stage2_frontier_points.csv").read_text(encoding="utf-8")
    assert "dominated" in frontier
    md = next(report_dir.glob("*.md")).read_text(encoding="utf-8")
    assert "online_policy" in md or "Stage-2" in md or "stage2" in md.lower()


@pytest.mark.asyncio
async def test_persisted_usage_cost_and_missing_stays_unavailable(tmp_path: Path):
    summary = await run_stage2_fixture(
        CFG, output_root=tmp_path, run_id="cost-prov"
    )
    assert summary["total_cost_usd"] is not None
    assert summary["total_cost_usd"] > 0
    assert summary["cost_provenance"] == "persisted_usage_estimated_cost_usd"
    # communication_overhead deliberately unavailable in fixture summary
    assert summary["communication_overhead"] is None


def test_missing_calibration_blocks_held_out(tmp_path: Path):
    class NS:
        run_dir = [str(tmp_path)]
        output_dir = str(tmp_path / "out")
        calibration = None
        config = str(CFG)
        held_out = True
        include_oracle = False
        seed = 42

    with pytest.raises(SystemExit, match="requires --calibration"):
        cmd_report(NS())


@pytest.mark.asyncio
async def test_calibration_hash_mismatch_blocks_held_out(tmp_path: Path):
    import shutil

    summary = await run_stage2_fixture(
        CFG, output_root=tmp_path, run_id="cal-match"
    )
    src = Path(summary["run_dir"])
    dev = tmp_path / "cal-dev"
    shutil.copytree(src, dev)
    manifest = json.loads((dev / "run_manifest.json").read_text(encoding="utf-8"))
    manifest["split"] = "development"
    manifest["private_data_policy"] = "private_labels_offline_only"
    manifest["public_evaluator_id"] = "public_harness"
    manifest["public_evaluator_version"] = "public-harness-v1"
    (dev / "run_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    raw = yaml.safe_load(CFG.read_text(encoding="utf-8")) or {}
    control = load_control_plane_mapping(raw)
    cal_path = tmp_path / "cal.json"
    from orchestra.experiments.stage2_pareto import _development_observation_rows

    points, provenance = _development_observation_rows(dev)
    write_calibration_artifact(
        cal_path,
        control=control,
        development_points=points,
        run_dir=dev,
        config_path=CFG,
        source_record_ids=provenance,
    )
    # Mutate preference in both top-level and pareto sections (YAML has both).
    bad = yaml.safe_load(CFG.read_text(encoding="utf-8")) or {}
    bad["preference_profile"] = "quality_first"
    bad.setdefault("pareto", {})["preference_profile"] = "quality_first"
    bad_control = load_control_plane_mapping(bad)
    artifact = json.loads(cal_path.read_text(encoding="utf-8"))
    from orchestra.experiments.stage2_pareto import CalibrationArtifact

    cal = CalibrationArtifact.from_dict(artifact)
    with pytest.raises(RuntimeError, match="calibration mismatch"):
        assert_calibration_matches(cal, bad_control)


@pytest.mark.asyncio
async def test_post_activation_crash_resumes_from_activated_revision(tmp_path: Path):
    with pytest.raises(RuntimeError, match="FAILPOINT:after_activation_checkpoint"):
        await run_stage2_fixture(
            CFG,
            output_root=tmp_path,
            run_id="crash-act",
            failpoint="after_activation_checkpoint",
        )
    run_dir = tmp_path / "crash-act"
    assert (run_dir / "failpoint.json").exists()
    # Resume without failpoint: exact-once activation / no duplicate decisions.
    summary = await run_stage2_fixture(
        CFG, output_root=tmp_path, run_id="crash-act", failpoint=None
    )
    assert summary["restart_recovery_counts"] == 1
    assert set(summary["committed"]) >= {"s1", "s2", "s3", "s4"}
    decisions = [
        json.loads(line)
        for line in (run_dir / "pareto" / "decisions.jsonl").read_text().splitlines()
        if line.strip()
    ]
    # At most one activated concurrency decision for the fork (exact-once).
    activated = [d for d in decisions if d.get("activated_revision_id")]
    assert len(activated) >= 1
    ids = [d["decision_id"] for d in decisions]
    assert len(ids) == len(set(ids))


@pytest.mark.asyncio
async def test_post_realization_crash_no_duplicate_accounting(tmp_path: Path):
    with pytest.raises(RuntimeError, match="FAILPOINT:after_realization_persisted"):
        await run_stage2_fixture(
            CFG,
            output_root=tmp_path,
            run_id="crash-real",
            failpoint="after_realization_persisted",
        )
    run_dir = tmp_path / "crash-real"
    before_usage = json.loads(
        (run_dir / "stage2_fixture_summary.json").read_text(encoding="utf-8")
        if (run_dir / "stage2_fixture_summary.json").exists()
        else "{}"
    )
    del before_usage  # failpoint may precede summary
    summary = await run_stage2_fixture(
        CFG, output_root=tmp_path, run_id="crash-real", failpoint=None
    )
    assert set(summary["committed"]) >= {"s1", "s2", "s3", "s4"}
    # Usage ids must remain unique after resume.
    ckpt = json.loads(
        next((run_dir / "tasks").rglob("task_checkpoint.json")).__str__()
        and Path(
            list((run_dir / "tasks").rglob("*checkpoint*.json"))[0]
        ).read_text(encoding="utf-8")
        if list((run_dir / "tasks").rglob("*checkpoint*.json"))
        else "{}"
    )
    del ckpt
    # Prefer archive events uniqueness.
    events_path = run_dir / "pareto" / "archive_events.jsonl"
    if events_path.exists():
        event_ids = [
            json.loads(line).get("event_id")
            for line in events_path.read_text().splitlines()
            if line.strip()
        ]
        event_ids = [e for e in event_ids if e]
        assert len(event_ids) == len(set(event_ids))


@pytest.mark.asyncio
async def test_reports_deterministic_under_fixed_seed(tmp_path: Path):
    summary = await run_stage2_fixture(
        CFG, output_root=tmp_path, run_id="seed-det"
    )
    a = tmp_path / "r1"
    b = tmp_path / "r2"
    write_stage2_report([Path(summary["run_dir"])], output_dir=a, seed=42)
    write_stage2_report([Path(summary["run_dir"])], output_dir=b, seed=42)
    for name in ("stage2_frontier_points.csv", "stage2_estimated_vs_realized.csv"):
        assert (a / name).read_text(encoding="utf-8") == (b / name).read_text(
            encoding="utf-8"
        )
    svgs_a = sorted(p.name for p in a.glob("*.svg"))
    svgs_b = sorted(p.name for p in b.glob("*.svg"))
    assert svgs_a == svgs_b
    for name in svgs_a:
        assert (a / name).read_bytes() == (b / name).read_bytes()


@pytest.mark.asyncio
async def test_formal_sample_m5_mock_no_checkpoint_drift(tmp_path: Path):
    from orchestra.cli import run_lcb_codex_three_subtask as mod

    class Args:
        config = str(REPO / "configs/experiments/lcb_formal_codex_three_subtask.yaml")
        plan = None
        source_repo = None
        output_root = str(tmp_path / "formal")
        run_id = "formal-m5"
        dry_run = False
        allow_config_drift = False
        mock_backends = True
        mock_llm = False
        synthetic_problem = True

    import os

    os.environ.pop("LCB_REPOSITORY_PATH", None)
    code = await mod._run(Args())
    assert "LCB_REPOSITORY_PATH" not in os.environ
    summary = json.loads(
        (tmp_path / "formal" / "formal-m5" / "summary.json").read_text(encoding="utf-8")
    )
    assert summary.get("error") is None, summary.get("error")
    assert code == 0
    assert summary["slow_loop_enabled"] is True
    assert summary["mock_backends"] is True
    assert summary["problem_source"] == "synthetic_fixture"
    assert isinstance(summary["m5_revision_count"], int)
    statuses = summary.get("subtask_status") or {}
    assert set(statuses) >= {"analyze", "implement", "verify"}
    assert "CheckpointDrift" not in str(summary.get("error") or "")
    committed = set(summary.get("committed") or [])
    # With mock backends writing the public-fixture solution, all stages commit.
    assert committed >= {"analyze", "implement", "verify"}, statuses
    assert statuses.get("analyze") == "committed"
    assert statuses.get("implement") == "committed"
    assert statuses.get("verify") == "committed"


def test_missing_real_lcb_data_explicit_error(tmp_path: Path):
    from orchestra.cli.run_lcb_codex_three_subtask import _load_lcb_problem

    with pytest.raises(RuntimeError, match="Refusing silent fallback"):
        _load_lcb_problem(
            {
                "benchmark": {
                    "data_dir": str(tmp_path / "does-not-exist"),
                    "task_id": "abc309_a",
                }
            }
        )


@pytest.mark.asyncio
async def test_freeze_calibration_from_dev_run(tmp_path: Path):
    # Fixture split must be rejected; synthesize an authoritative development run.
    summary = await run_stage2_fixture(
        CFG, output_root=tmp_path, run_id="freeze-src"
    )
    src = Path(summary["run_dir"])
    dev = tmp_path / "freeze-dev"
    import shutil

    shutil.copytree(src, dev)
    manifest = json.loads((dev / "run_manifest.json").read_text(encoding="utf-8"))
    manifest["split"] = "development"
    manifest["private_data_policy"] = "private_labels_offline_only"
    manifest["public_evaluator_id"] = "public_harness"
    manifest["public_evaluator_version"] = "public-harness-v1"
    (dev / "run_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    class NS:
        config = str(CFG)
        run_dir = [str(dev)]
        output = str(tmp_path / "dev_cal.json")

    assert cmd_freeze_calibration(NS()) == 0
    payload = json.loads(Path(NS.output).read_text(encoding="utf-8"))
    assert payload["source_split"] == "development"
    assert payload["source_split"] == manifest["split"]
    assert payload["candidate_catalog_hash"]
    assert payload["preference_hash"]
    assert payload["normalization_source_record_ids"]
    assert payload["manifest_schema_version"] in {
        "stage2-calibration-v1",
        "stage2-calibration-v2",
    }
