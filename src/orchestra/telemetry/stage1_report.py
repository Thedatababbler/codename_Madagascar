"""Cross-baseline Stage 1 comparison reports."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from orchestra.experiments.stage1 import BASELINE_ORDER, PhaseName, phase_output_root
from orchestra.telemetry.summary import summarize_run

BASELINE_LABELS = {
    "b0": "B0 Direct",
    "b1": "B1 Single+Harness",
    "b2": "B2 Fixed MAS",
}


def _latest_run_dir(phase: PhaseName, baseline: str) -> Path | None:
    root = Path(phase_output_root(phase, baseline))
    if not root.exists():
        return None
    candidates = [path for path in root.iterdir() if path.is_dir()]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def collect_phase_summaries(phase: PhaseName) -> dict[str, dict]:
    summaries = {}
    for baseline in BASELINE_ORDER:
        run_dir = _latest_run_dir(phase, baseline)
        if run_dir is None:
            continue
        summaries[baseline] = summarize_run(run_dir)
        summaries[baseline]["run_dir"] = str(run_dir)
    return summaries


def write_main_results_table(phase: PhaseName, output_dir: str | Path) -> Path:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    summaries = collect_phase_summaries(phase)
    metrics = [
        ("hidden_pass@1_strict", "Hidden Pass@1"),
        ("public_pass_before_repair", "Public pass before repair"),
        ("public_pass_after_repair", "Public pass after repair"),
        ("compile_success_rate", "Compile success rate"),
        ("runtime_error_count", "Runtime error count"),
        ("harness_timeout_count", "Code timeout count"),
        ("infrastructure_error_rate", "Infrastructure error rate"),
        ("repair_trigger_rate", "Repair trigger rate"),
        ("repair_success_rate", "Repair success rate"),
        ("avg_prompt_tokens_per_task", "Avg. prompt tokens"),
        ("avg_completion_tokens_per_task", "Avg. completion tokens"),
        ("avg_cost_per_task_usd", "Avg. total cost"),
        ("cost_per_solved_task_usd", "Cost per solved task"),
        ("average_graph_wall_latency_ms", "Avg. wall latency"),
        ("average_concurrency_speedup", "B2 parallel speedup"),
    ]
    path = output / f"stage1_{phase}_main_results.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Metric", *[BASELINE_LABELS[b] for b in BASELINE_ORDER]])
        for key, label in metrics:
            row = [label]
            for baseline in BASELINE_ORDER:
                value = summaries.get(baseline, {}).get(key)
                row.append("" if value is None else value)
            writer.writerow(row)
    return path


def write_difficulty_table(phase: PhaseName, output_dir: str | Path) -> Path:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    summaries = collect_phase_summaries(phase)
    difficulties = sorted(
        {
            difficulty
            for summary in summaries.values()
            for difficulty in (summary.get("by_difficulty") or {})
        }
    )
    path = output / f"stage1_{phase}_by_difficulty.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["Difficulty", "B0 Pass@1", "B1 Pass@1", "B2 Pass@1"]
        )
        for difficulty in difficulties:
            row = [difficulty]
            for baseline in BASELINE_ORDER:
                by_diff = summaries.get(baseline, {}).get("by_difficulty", {})
                row.append(by_diff.get(difficulty, {}).get("hidden_pass@1", ""))
            writer.writerow(row)
    return path


def write_task_comparison_table(phase: PhaseName, output_dir: str | Path) -> Path:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    run_dirs = {
        baseline: _latest_run_dir(phase, baseline) for baseline in BASELINE_ORDER
    }
    task_ids = set()
    task_meta: dict[str, dict] = {}
    per_baseline: dict[str, dict[str, bool | None]] = {b: {} for b in BASELINE_ORDER}
    for baseline, run_dir in run_dirs.items():
        if run_dir is None:
            continue
        manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
        for entry in manifest["manifest"]["entries"]:
            task_ids.add(entry["question_id"])
            task_meta[entry["question_id"]] = entry
        for task_dir in (run_dir / "tasks").glob("*"):
            evaluation = task_dir / "final_evaluation.json"
            if evaluation.exists():
                payload = json.loads(evaluation.read_text(encoding="utf-8"))
                per_baseline[baseline][task_dir.name] = bool(payload.get("passed"))
            else:
                per_baseline[baseline][task_dir.name] = None
    path = output / f"stage1_{phase}_task_comparison.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["Task ID", "Difficulty", "B0", "B1", "B2", "Cheapest Successful"]
        )
        for task_id in sorted(task_ids):
            meta = task_meta.get(task_id, {})
            row = [
                task_id,
                meta.get("difficulty", ""),
            ]
            passes = []
            for baseline in BASELINE_ORDER:
                value = per_baseline[baseline].get(task_id)
                row.append("" if value is None else int(value))
                if value:
                    passes.append(baseline)
            row.append(passes[0] if len(passes) == 1 else ",".join(passes))
            writer.writerow(row)
    return path


def write_phase_report(phase: PhaseName, output_dir: str | Path | None = None) -> dict[str, Path]:
    reports_root = Path(output_dir or "outputs/stage1_experiments/reports")
    return {
        "main_results": write_main_results_table(phase, reports_root),
        "by_difficulty": write_difficulty_table(phase, reports_root),
        "task_comparison": write_task_comparison_table(phase, reports_root),
    }
