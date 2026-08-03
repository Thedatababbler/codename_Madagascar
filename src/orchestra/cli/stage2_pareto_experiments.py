"""Stage-2 Pareto experiment CLI (validate / generate / dry-run / fixture / report).

Does not launch paid API or held-out experiments automatically.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import yaml

from orchestra.experiments.control_plane import load_control_plane_mapping, resolve_pareto_runtime
from orchestra.experiments.stage2_fixture import run_stage2_fixture
from orchestra.experiments.stage2_pareto import (
    MODE_ORDER,
    CalibrationArtifact,
    CalibrationFreezeError,
    CalibrationMismatchError,
    _development_observation_rows,
    assert_calibration_matches,
    validate_stage2_config,
    write_all_stage2_configs,
    write_calibration_artifact,
    write_stage2_report,
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def cmd_validate(args: argparse.Namespace) -> int:
    summary = validate_stage2_config(args.config)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


def cmd_generate_configs(args: argparse.Namespace) -> int:
    out = Path(args.output_dir) if args.output_dir else _repo_root() / "configs/experiments/stage2"
    paths = write_all_stage2_configs(out)
    for path in paths:
        print(path)
    return 0


def cmd_dry_run(args: argparse.Namespace) -> int:
    raw = yaml.safe_load(Path(args.config).read_text(encoding="utf-8")) or {}
    control = load_control_plane_mapping(raw)
    resolved = resolve_pareto_runtime(control, repo_root=_repo_root())
    payload = {
        "mode": "dry-run",
        "config_path": str(args.config),
        "pareto_enabled": resolved.pareto_config.enabled,
        "slow_loop_enabled": resolved.slow_loop_config.enabled,
        "preference_profile": resolved.preference_profile.profile_id,
        "preference_hash": resolved.preference_hash,
        "objective_hash": resolved.objective_hash,
        "pricing_version": resolved.pricing_version,
        "control_plane_hash": resolved.control_plane_hash,
        "pareto_config": resolved.pareto_config.model_dump(mode="json"),
        "candidate_catalog": resolved.candidate_catalog.model_dump(mode="json"),
        "modes_available": list(MODE_ORDER),
        "note": "No tasks executed; no paid API calls.",
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def cmd_run_fixture(args: argparse.Namespace) -> int:
    summary = asyncio.run(
        run_stage2_fixture(
            args.config,
            output_root=args.output_root,
            run_id=args.run_id,
        )
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    if resolved_requires_pareto_selection(args.config) and not summary.get("selected_hash"):
        # Soft warning only when mode expects Pareto selection.
        if summary.get("pareto_enabled"):
            print(
                "warning: pareto enabled but no selected_hash; inspect run_dir artifacts",
                flush=True,
            )
    return 0


def resolved_requires_pareto_selection(config_path: str | Path) -> bool:
    raw = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
    return bool((raw.get("pareto") or {}).get("enabled"))


def cmd_report(args: argparse.Namespace) -> int:
    from orchestra.experiments.stage2_pareto import assert_run_split_for_held_out

    run_dirs = [Path(p) for p in args.run_dir]
    output = Path(args.output_dir or "outputs/stage2_pareto/reports")
    calibration = None
    if args.held_out:
        if not args.calibration:
            raise SystemExit(
                "held-out reporting requires --calibration <frozen-calibration.json>"
            )
        if not args.config:
            raise SystemExit(
                "held-out reporting requires --config <matching-experiment-config>"
            )
        try:
            calibration = CalibrationArtifact.from_dict(
                json.loads(Path(args.calibration).read_text(encoding="utf-8"))
            )
        except Exception as exc:  # noqa: BLE001
            raise SystemExit(f"malformed calibration artifact: {exc}") from exc
        raw = yaml.safe_load(Path(args.config).read_text(encoding="utf-8")) or {}
        control = load_control_plane_mapping(raw)
        for run_dir in run_dirs:
            try:
                manifest = assert_run_split_for_held_out(run_dir)
            except RuntimeError as exc:
                raise SystemExit(str(exc)) from exc
            try:
                assert_calibration_matches(
                    calibration,
                    control,
                    require_held_out_split=True,
                    run_manifest=manifest,
                )
            except (CalibrationMismatchError, RuntimeError) as exc:
                raise SystemExit(str(exc)) from exc
    elif args.calibration:
        calibration = CalibrationArtifact.from_dict(
            json.loads(Path(args.calibration).read_text(encoding="utf-8"))
        )
        if args.config:
            raw = yaml.safe_load(Path(args.config).read_text(encoding="utf-8")) or {}
            control = load_control_plane_mapping(raw)
            try:
                assert_calibration_matches(calibration, control)
            except (CalibrationMismatchError, RuntimeError) as exc:
                raise SystemExit(str(exc)) from exc
    payload = write_stage2_report(
        run_dirs,
        output_dir=output,
        calibration=calibration,
        seed=int(args.seed),
        include_oracle=bool(args.include_oracle),
    )
    print(json.dumps({"report_dir": str(output), "run_count": payload["run_count"]}, indent=2))
    return 0


def cmd_freeze_calibration(args: argparse.Namespace) -> int:
    raw = yaml.safe_load(Path(args.config).read_text(encoding="utf-8")) or {}
    control = load_control_plane_mapping(raw)
    if not args.run_dir:
        raise SystemExit(
            "freeze-calibration requires --run-dir with a persisted development run"
        )
    points: list[dict[str, float | None]] = []
    provenance: dict[str, list[str]] = {}
    primary = Path(args.run_dir[0])
    for run_dir in args.run_dir:
        try:
            rows, source_ids = _development_observation_rows(Path(run_dir))
        except CalibrationFreezeError as exc:
            raise SystemExit(str(exc)) from exc
        split = ""
        manifest_path = Path(run_dir) / "run_manifest.json"
        if manifest_path.exists():
            split = str(
                json.loads(manifest_path.read_text(encoding="utf-8")).get("split") or ""
            )
        if split != "development":
            raise SystemExit(
                "freeze-calibration may consume development runs only; "
                f"refusing split={split!r} under {run_dir}. "
                "A CLI command must never relabel a run's persisted split."
            )
        points.extend(rows)
        for name, ids in source_ids.items():
            provenance.setdefault(name, []).extend(ids)
    for name, ids in list(provenance.items()):
        provenance[name] = sorted(set(ids))
    if not points:
        raise SystemExit(
            "freeze-calibration refused: no finite development objective observations "
            "with provenance; refusing fabricated normalization defaults"
        )
    try:
        artifact = write_calibration_artifact(
            args.output,
            control=control,
            development_points=points,
            run_dir=primary,
            config_path=args.config,
            source_record_ids=provenance,
        )
    except (CalibrationFreezeError, RuntimeError) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(artifact.to_dict(), indent=2, sort_keys=True))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_val = sub.add_parser("validate", help="Validate Stage-2 YAML via shared typed loader")
    p_val.add_argument("--config", required=True)
    p_val.set_defaults(func=cmd_validate)

    p_gen = sub.add_parser("generate-configs", help="Write comparable Stage-2 mode YAMLs")
    p_gen.add_argument("--output-dir", default=None)
    p_gen.set_defaults(func=cmd_generate_configs)

    p_dry = sub.add_parser("dry-run", help="Resolve config without executing tasks")
    p_dry.add_argument("--config", required=True)
    p_dry.set_defaults(func=cmd_dry_run)

    p_fix = sub.add_parser(
        "run-fixture",
        help="Deterministic multi-subtask fixture (no paid API)",
    )
    p_fix.add_argument("--config", required=True)
    p_fix.add_argument("--output-root", default=None)
    p_fix.add_argument("--run-id", default=None)
    p_fix.set_defaults(func=cmd_run_fixture)

    p_rep = sub.add_parser("report", help="Build reports from run artifacts")
    p_rep.add_argument("--run-dir", action="append", required=True)
    p_rep.add_argument("--output-dir", default="outputs/stage2_pareto/reports")
    p_rep.add_argument("--calibration", default=None)
    p_rep.add_argument("--config", default=None)
    p_rep.add_argument("--held-out", action="store_true")
    p_rep.add_argument("--include-oracle", action="store_true")
    p_rep.add_argument("--seed", type=int, default=42)
    p_rep.set_defaults(func=cmd_report)

    p_cal = sub.add_parser(
        "freeze-calibration",
        help="Write frozen development calibration artifact",
    )
    p_cal.add_argument("--config", required=True)
    p_cal.add_argument("--run-dir", action="append", default=[])
    p_cal.add_argument(
        "--output",
        default="outputs/stage2_pareto/calibration/dev_calibration.json",
    )
    p_cal.set_defaults(func=cmd_freeze_calibration)

    args = parser.parse_args()
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
