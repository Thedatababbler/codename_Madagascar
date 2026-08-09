#!/usr/bin/env python3
"""Aggregate the decomposition A/B into a per-repository arm comparison.

Reports the within-repository difference between arms. Absolute held-out numbers
on this dataset include tests whose imports the specification never states, so
the reachable-normalised rate is the primary column (see EXP-20260808-03).
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
from functools import cache
from pathlib import Path

from orchestra.codeprojecteval.ceiling import (
    CeilingReport,
    analyze_ceiling,
    collected_counts,
)
from orchestra.codeprojecteval.dataset import DEFAULT_ENV_ROOT, load_task


@cache
def _ceiling(task_id: str) -> CeilingReport:
    task = load_task(task_id)
    return analyze_ceiling(
        task,
        collected=collected_counts(
            task,
            python=DEFAULT_ENV_ROOT / task_id / "bin" / "python",
            cache_path=Path("outputs/cpe_collect_cache.json"),
        ),
    )


def _collect(root: Path) -> dict[tuple[str, str], list[dict]]:
    runs: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for batch in sorted(p for p in root.iterdir() if p.is_dir()):
        summary_path = batch / "batch_summary.json"
        hidden_path = batch / "hidden_eval.json"
        if not summary_path.is_file():
            continue
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        for result in summary.get("results") or []:
            task = result.get("task_id")
            arm = result.get("arm") or "unknown"
            entry = {
                "batch": batch.name,
                "milestones": result.get("milestone_count"),
                "agent_turns": result.get("agent_turns"),
                "committed": len(result.get("committed") or []),
                "run_error": result.get("error"),
            }
            if hidden_path.is_file():
                hidden = json.loads(hidden_path.read_text(encoding="utf-8"))
                for scored in hidden.get("results") or []:
                    if scored.get("task_id") != task:
                        continue
                    passed = int(scored.get("passed") or 0)
                    ceiling = _ceiling(task)
                    entry.update(
                        {
                            "passed": passed,
                            "failed": scored.get("failed"),
                            "error": scored.get("error"),
                            # Recomputed here so older result files pick up the
                            # collected-test denominator rather than a static
                            # count of `def test_*`.
                            "pass_rate": round(passed / ceiling.tests_total, 4)
                            if ceiling.tests_total
                            else 0.0,
                            # Optimistic bound: a module predicted unreachable
                            # still runs if the agent happened to define the
                            # name, so this can saturate. Clamped, not primary.
                            "pass_rate_reachable": round(
                                min(1.0, passed / ceiling.tests_reachable), 4
                            )
                            if ceiling.tests_reachable
                            else 0.0,
                            "ceiling": ceiling.reachable_ceiling,
                        }
                    )
            runs[(task, arm)].append(entry)
    return runs


def _mean(values: list[float]) -> float:
    clean = [v for v in values if v is not None]
    return round(statistics.mean(clean), 4) if clean else 0.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("root", type=Path, nargs="?", default=Path("outputs/cpe_ab"))
    ap.add_argument("--out", type=Path, default=Path("outputs/cpe_ab/ab_summary.json"))
    args = ap.parse_args()

    runs = _collect(args.root)
    tasks = sorted({task for task, _ in runs})
    report: dict[str, dict] = {}

    header = (
        f"{'repo':<14}{'arm':<8}{'n':>2}  {'milestones':>10}  {'turns':>5}  "
        f"{'pass_rate':>9}  {'bound':>6}  {'committed':>9}"
    )
    print(header)
    print("-" * len(header))
    for task in tasks:
        report[task] = {}
        for arm in ("single", "multi"):
            entries = runs.get((task, arm)) or []
            if not entries:
                continue
            stats = {
                "n": len(entries),
                "milestones": _mean([e.get("milestones") for e in entries]),
                "agent_turns": _mean([e.get("agent_turns") for e in entries]),
                "pass_rate_reachable": _mean(
                    [e.get("pass_rate_reachable") for e in entries]
                ),
                "pass_rate": _mean([e.get("pass_rate") for e in entries]),
                "committed": _mean([e.get("committed") for e in entries]),
                "errors": [e["run_error"] for e in entries if e.get("run_error")],
                "runs": entries,
            }
            report[task][arm] = stats
            print(
                f"{task:<14}{arm:<8}{stats['n']:>2}  {stats['milestones']:>10.1f}  "
                f"{stats['agent_turns']:>5.1f}  {stats['pass_rate']:>9.3f}  "
                f"{stats['pass_rate_reachable']:>6.3f}  {stats['committed']:>9.1f}"
            )
        both = report[task]
        if "single" in both and "multi" in both:
            delta = round(
                both["multi"]["pass_rate"] - both["single"]["pass_rate"], 4
            )
            both["delta_pass_rate_multi_minus_single"] = delta
            print(f"{'':<14}{'delta':<8}{'':>2}  {'':>10}  {'':>5}  {delta:>+9.3f}")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
