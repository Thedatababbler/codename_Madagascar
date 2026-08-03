"""M6.2 typed control-plane loader and Stage-1 backward compatibility."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from orchestra.config import load_experiment_config
from orchestra.control.pareto.runtime_factory import (
    reported_pareto_config,
    resolve_from_mapping,
    resolve_from_path,
)
from orchestra.control.pareto.schemas import ObjectiveDirection, ParetoConfig
from orchestra.experiments.control_plane import (
    ControlPlaneConfig,
    load_control_plane_mapping,
    resolve_pareto_runtime,
)
from orchestra.experiments.stage2_pareto import (
    assert_calibration_matches,
    build_mode_config,
    write_calibration_artifact,
    write_stage2_report,
)

REPO = Path(__file__).resolve().parents[2]


def test_stage1_config_remains_valid_and_pareto_disabled():
    cfg = load_experiment_config(REPO / "configs/experiments/stage1_b0_direct.yaml")
    assert cfg.experiment.name == "stage1_b0_direct"
    assert not bool((cfg.pareto or {}).get("enabled", False))
    assert not bool((cfg.slow_loop or {}).get("enabled", False))


def test_pareto_requires_slow_loop():
    with pytest.raises(ValueError, match="requires slow_loop.enabled"):
        load_control_plane_mapping(
            {"slow_loop": {"enabled": False}, "pareto": {"enabled": True}}
        )


def test_unknown_objective_rejected():
    with pytest.raises(ValueError, match="unknown Pareto objectives"):
        load_control_plane_mapping(
            {
                "slow_loop": {"enabled": True},
                "pareto": {
                    "enabled": True,
                    "objectives": {"not_a_real_objective": "minimize"},
                },
            }
        )


def test_missing_preference_profile_rejected():
    with pytest.raises(ValueError, match="missing preference profile"):
        resolve_pareto_runtime(
            ControlPlaneConfig.model_validate(
                {
                    "slow_loop": {"enabled": True},
                    "pareto": {
                        "enabled": True,
                        "preference_profile": "does_not_exist",
                    },
                    "preference_profile": "does_not_exist",
                }
            ),
            repo_root=REPO,
        )


def test_unsafe_graph_path_rejected(tmp_path: Path):
    bad = tmp_path / "catalog.yaml"
    # Point catalog at a traversal-style path that must be rejected.
    with pytest.raises(ValueError, match="does not exist|unsafe"):
        resolve_pareto_runtime(
            load_control_plane_mapping(
                {
                    "slow_loop": {"enabled": True},
                    "pareto": {"enabled": True, "preference_profile": "balanced_knee"},
                    "candidate_catalog": {
                        "graph_templates": [
                            {
                                "template_id": "bad",
                                "graph_path": "../etc/passwd",
                                "target_roles": ["s2"],
                            }
                        ]
                    },
                }
            ),
            repo_root=REPO,
        )
    del bad


def test_production_yaml_constructs_exact_pareto_config():
    path = REPO / "configs/experiments/stage2/m6_balanced_knee.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    control = load_control_plane_mapping(raw)
    reported = reported_pareto_config(control)
    resolved = resolve_from_path(path, repo_root=REPO)
    assert reported == resolved.pareto_config
    assert isinstance(reported, ParetoConfig)
    assert reported.enabled is True
    assert reported.objectives["quality"] is ObjectiveDirection.MAXIMIZE
    assert reported.allow_two_edit_pairs is True


def test_smoke_and_stage2_share_loader():
    smoke = resolve_from_mapping(
        {
            "slow_loop": {"enabled": True},
            "pareto": {
                "enabled": True,
                "max_candidates": 8,
                "preference_profile": "balanced_knee",
            },
            "preference_profile": "balanced_knee",
        },
        repo_root=REPO,
    )
    stage2 = resolve_from_path(
        REPO / "configs/experiments/stage2/m6_balanced_knee.yaml",
        repo_root=REPO,
    )
    assert smoke.pareto_config.enabled == stage2.pareto_config.enabled
    assert smoke.preference_profile.profile_id == "balanced_knee"
    assert stage2.preference_hash
    assert stage2.objective_hash
    assert stage2.pricing_version


def test_calibration_mismatch_blocks_held_out(tmp_path: Path):
    import json

    control = load_control_plane_mapping(build_mode_config("m6_balanced_knee"))
    run = tmp_path / "dev"
    run.mkdir()
    (run / "run_manifest.json").write_text(
        json.dumps(
            {
                "split": "development",
                "run_id": "dev-cal",
                "started_at": "2020-01-01T00:00:00+00:00",
                "private_data_policy": "private_labels_offline_only",
                "public_evaluator_id": "public_harness",
                "public_evaluator_version": "public-harness-v1",
            }
        ),
        encoding="utf-8",
    )
    points = [
        {
            "quality": 1.0,
            "cost": 0.1,
            "latency": 1.0,
            "risk": 0.1,
            "communication_overhead": 1.0,
        }
    ]
    artifact = write_calibration_artifact(
        tmp_path / "cal.json",
        control=control,
        development_points=points,
        run_dir=run,
        source_record_ids={k: ["dec-1"] for k in points[0]},
    )
    other = load_control_plane_mapping(build_mode_config("m6_quality_first"))
    with pytest.raises(RuntimeError, match="calibration mismatch"):
        assert_calibration_matches(artifact, other)


def test_report_deterministic_and_missing_cost_not_zero(tmp_path: Path):
    run_a = tmp_path / "run_a"
    run_b = tmp_path / "run_b"
    for run in (run_a, run_b):
        (run / "pareto").mkdir(parents=True)
        (run / "run_manifest.json").write_text(
            '{"preference_profile_id":"balanced_knee","stage2":{"mode":"m6_balanced_knee"}}',
            encoding="utf-8",
        )
        (run / "stage2_fixture_summary.json").write_text(
            '{"mode":"m6_balanced_knee","committed":["s1","s2"],'
            '"execution_success_rate":1.0,"avg_cost_usd":null}',
            encoding="utf-8",
        )
        (run / "pareto" / "estimated_archive.json").write_text(
            '{"estimated_complete":{"ctx":[{"content_hash":"abc",'
            '"objectives":{"values":{"quality":{"value":0.9,"available":true},'
            '"cost":{"value":null,"available":false},"latency":{"value":1.0,"available":true}}}}]},'
            '"estimated_partial":{}}',
            encoding="utf-8",
        )
        (run / "pareto" / "decisions.jsonl").write_text(
            '{"decision_id":"d1","selection_status":"selected_complete_frontier",'
            '"selected_content_hash":"abc","evaluation_kind":"estimated"}\n',
            encoding="utf-8",
        )
    out1 = tmp_path / "rep1"
    out2 = tmp_path / "rep2"
    write_stage2_report([run_a], output_dir=out1, seed=42)
    write_stage2_report([run_a], output_dir=out2, seed=42)
    assert (out1 / "stage2_main_results.csv").read_text() == (
        out2 / "stage2_main_results.csv"
    ).read_text()
    assert (out1 / "pareto_quality_cost.svg").exists()
    frontier = (out1 / "stage2_frontier_points.csv").read_text(encoding="utf-8")
    # Missing cost must remain empty, not coerced to 0.
    assert ",0.9,,1.0," in frontier.replace("True", "True") or ",0.9,,1.0" in frontier
    assert ",0.9,0,1.0" not in frontier
    assert ",0.9,0.0,1.0" not in frontier


def test_oracle_labeled_diagnostic_excluded(tmp_path: Path):
    run = tmp_path / "run"
    (run / "pareto").mkdir(parents=True)
    (run / "run_manifest.json").write_text("{}", encoding="utf-8")
    (run / "stage2_fixture_summary.json").write_text(
        '{"mode":"diag","committed":[]}', encoding="utf-8"
    )
    (run / "pareto" / "decisions.jsonl").write_text(
        '{"decision_id":"oracle1","selection_status":"selected_complete_frontier",'
        '"selected_content_hash":"x","evaluation_kind":"oracle"}\n',
        encoding="utf-8",
    )
    (run / "pareto" / "estimated_archive.json").write_text(
        '{"estimated_complete":{},"estimated_partial":{}}', encoding="utf-8"
    )
    out = tmp_path / "rep"
    write_stage2_report([run], output_dir=out, include_oracle=False)
    decisions = (out / "stage2_decision_summary.csv").read_text(encoding="utf-8")
    assert "oracle1" not in decisions
    failures = (out / "stage2_failures.csv").read_text(encoding="utf-8")
    assert "oracle_diagnostic_excluded" in failures
