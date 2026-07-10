"""Stage 1 experiment orchestration CLI."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from orchestra.adapters.livecodebench.mapper import load_manifest
from orchestra.cli.evaluate import _evaluate
from orchestra.experiments.stage1 import (
    BASELINE_ORDER,
    BaselineName,
    PhaseName,
    build_phase_config,
    write_all_phase_configs,
)
from orchestra.telemetry.stage1_report import write_phase_report
from orchestra.telemetry.summary import summarize_run


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _run_command(command: list[str], *, cwd: Path | None = None) -> None:
    print("$", " ".join(command), flush=True)
    subprocess.run(command, cwd=cwd or _repo_root(), check=True)


def _phase_config_path(phase: PhaseName, baseline: BaselineName) -> Path:
    return _repo_root() / "configs/experiments/stage1" / f"{phase}_{baseline}.yaml"


def _ensure_phase_configs() -> None:
    write_all_phase_configs(_repo_root() / "configs/experiments/stage1")


def _latest_run_dir(phase: PhaseName, baseline: BaselineName) -> Path:
    config = build_phase_config(phase, baseline)
    root = _repo_root() / config.experiment.output_root
    candidates = [path for path in root.iterdir() if path.is_dir()] if root.exists() else []
    if not candidates:
        raise FileNotFoundError(f"No runs found under {root}")
    return max(candidates, key=lambda path: path.stat().st_mtime)


async def _evaluate_run(run_dir: Path) -> int:
    return await _evaluate(run_dir)


def _count_frozen_tasks(run_dir: Path) -> tuple[int, int]:
    task_root = run_dir / "tasks"
    if not task_root.exists():
        return 0, 0
    total = 0
    frozen = 0
    for task_dir in task_root.iterdir():
        if not task_dir.is_dir():
            continue
        total += 1
        graph_result = task_dir / "graph_result.json"
        if not graph_result.exists():
            continue
        payload = json.loads(graph_result.read_text(encoding="utf-8"))
        if payload.get("state", {}).get("frozen"):
            frozen += 1
    return frozen, total


def cmd_generate_configs(_args: argparse.Namespace) -> int:
    paths = write_all_phase_configs(_repo_root() / "configs/experiments/stage1")
    for path in paths:
        print(path)
    return 0


def cmd_acceptance(args: argparse.Namespace) -> int:
    command = ["uv", "run", "pytest", "-q", "tests/unit", "tests/integration"]
    env_vars = dict(os.environ)
    if args.lcb_repository_path:
        env_vars["LCB_REPOSITORY_PATH"] = args.lcb_repository_path
    print("Phase A: executor acceptance", flush=True)
    subprocess.run(command, cwd=_repo_root(), check=True, env=env_vars)
    print("Phase A passed.", flush=True)
    return 0


def _run_baseline(
    phase: PhaseName,
    baseline: BaselineName,
    *,
    force_rerun: bool,
    mock_llm: bool,
) -> Path:
    _ensure_phase_configs()
    config_path = _phase_config_path(phase, baseline)
    command = [
        "uv",
        "run",
        "python",
        "-m",
        "orchestra.cli.run",
        "--config",
        str(config_path),
        "--force-rerun",
    ]
    if mock_llm:
        command.append("--mock-llm")
    elif force_rerun:
        pass
    _run_command(command)
    run_dir = _latest_run_dir(phase, baseline)
    frozen, total = _count_frozen_tasks(run_dir)
    print(f"{baseline}: frozen={frozen}/{total} run_dir={run_dir}", flush=True)
    if not mock_llm and frozen > 0:
        evaluate_code = asyncio.run(_evaluate_run(run_dir))
        if evaluate_code != 0:
            print(
                f"warning: evaluate reported incomplete tasks for {baseline}",
                flush=True,
            )
    summarize_run(run_dir)
    if frozen != total:
        raise RuntimeError(
            f"{phase}/{baseline} incomplete: frozen={frozen}/{total} under {run_dir}"
        )
    return run_dir


def cmd_bringup(args: argparse.Namespace) -> int:
    print("Phase B: 3-task bring-up (sequential B0 -> B1 -> B2)", flush=True)
    run_dirs = {}
    for baseline in BASELINE_ORDER:
        print(f"Running {baseline}...", flush=True)
        run_dirs[baseline] = _run_baseline(
            "bringup",
            baseline,
            force_rerun=args.force_rerun,
            mock_llm=args.mock_llm,
        )
    report = {
        "phase": "bringup",
        "completed_at": datetime.now(UTC).isoformat(),
        "runs": {baseline: str(path) for baseline, path in run_dirs.items()},
    }
    out = _repo_root() / "outputs/stage1_experiments/bringup/last_run.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


def _run_phase(phase: PhaseName, args: argparse.Namespace) -> int:
    run_dirs = {}
    for baseline in BASELINE_ORDER:
        print(f"Running {phase} / {baseline}...", flush=True)
        run_dirs[baseline] = _run_baseline(
            phase,
            baseline,
            force_rerun=args.force_rerun,
            mock_llm=args.mock_llm,
        )
    reports = write_phase_report(phase)
    payload = {
        "phase": phase,
        "completed_at": datetime.now(UTC).isoformat(),
        "runs": {baseline: str(path) for baseline, path in run_dirs.items()},
        "reports": {key: str(path) for key, path in reports.items()},
    }
    out = _repo_root() / f"outputs/stage1_experiments/{phase}/last_run.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(payload, indent=2))
    return 0


def cmd_smoke(args: argparse.Namespace) -> int:
    print("Phase C: 15-task smoke", flush=True)
    return _run_phase("smoke", args)


def cmd_dev(args: argparse.Namespace) -> int:
    print("Phase D: 60-task development", flush=True)
    return _run_phase("dev", args)


def cmd_heldout(args: argparse.Namespace) -> int:
    print("Phase E: 60-task held-out", flush=True)
    return _run_phase("heldout", args)


def cmd_report(args: argparse.Namespace) -> int:
    reports = write_phase_report(args.phase)
    for key, path in reports.items():
        print(f"{key}: {path}")
    return 0


def cmd_dry_run(args: argparse.Namespace) -> int:
    _ensure_phase_configs()
    for phase in ("bringup", "smoke", "dev", "heldout"):
        for baseline in BASELINE_ORDER:
            config = build_phase_config(phase, baseline)
            manifest = load_manifest(config.benchmark.manifest)
            print(
                json.dumps(
                    {
                        "phase": phase,
                        "baseline": baseline,
                        "config": str(_phase_config_path(phase, baseline)),
                        "manifest": config.benchmark.manifest,
                        "task_count": len(manifest.entries),
                        "output_root": config.experiment.output_root,
                        "runtime": config.runtime.model_dump(),
                    }
                )
            )
    return 0


def main(argv: list[str] | None = None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--force-rerun", action="store_true")
    common.add_argument("--mock-llm", action="store_true")

    parser = argparse.ArgumentParser(description="Stage 1 LiveCodeBench experiments")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("generate-configs", help="Write phase baseline YAML configs")
    acceptance = sub.add_parser(
        "acceptance", parents=[common], help="Phase A executor acceptance tests"
    )
    acceptance.add_argument("--lcb-repository-path", default=None)
    sub.add_parser("bringup", parents=[common], help="Phase B 3-task bring-up")
    sub.add_parser("smoke", parents=[common], help="Phase C 15-task smoke")
    sub.add_parser("dev", parents=[common], help="Phase D 60-task development")
    sub.add_parser("heldout", parents=[common], help="Phase E 60-task held-out")
    report = sub.add_parser("report", help="Generate comparison tables for a phase")
    report.add_argument(
        "--phase", choices=["bringup", "smoke", "dev", "heldout"], required=True
    )
    sub.add_parser("dry-run", help="Print planned phase/baseline configs")

    args = parser.parse_args(argv)
    if not hasattr(args, "force_rerun"):
        args.force_rerun = False
    if not hasattr(args, "mock_llm"):
        args.mock_llm = False
    handlers = {
        "generate-configs": cmd_generate_configs,
        "acceptance": cmd_acceptance,
        "bringup": cmd_bringup,
        "smoke": cmd_smoke,
        "dev": cmd_dev,
        "heldout": cmd_heldout,
        "report": cmd_report,
        "dry-run": cmd_dry_run,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
