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
from hashlib import sha1
from pathlib import Path

import yaml

from orchestra.codeprojecteval.ceiling import (
    CeilingReport,
    analyze_ceiling,
    collected_counts,
)
from orchestra.codeprojecteval.dataset import DEFAULT_ENV_ROOT, load_task

# Newest last; used only to break ties when an arm is mid-rerun.
_ENGINE_RECENCY = {"unknown": 0, "legacy_chain": 1, "role_pool": 2}


def _realized_turns(run_dir: Path) -> int | None:
    """Agent nodes that actually ran, counted from the event stream.

    The planned count overstates any template with an early exit: when
    ``gate_then_repair`` passes its mid-milestone probe, the repairer never
    runs, and reporting the budget would hide that the multi-segment arm bought
    its result with less compute than the control it is compared against.
    """
    turns = 0
    seen = False
    for events in sorted(Path(run_dir).glob("*/logs/*/events.jsonl")):
        for line in events.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            seen = True
            if event.get("event_type") == "NODE_COMPLETED" and str(
                event.get("node_id") or ""
            ).startswith("agent_"):
                turns += 1
    return turns if seen else None


def _engine(run_dir: Path) -> str:
    """Which subgraph builder produced this run's graphs.

    A frozen plan replayed under a newer builder is not a repeat of the same
    condition: the template-based builder injects each role's own prompt, so an
    arm holding runs from both is measuring two systems and calling it noise.
    """
    graphs = sorted(Path(run_dir).glob("*/generated/graphs/*.yaml"))
    if not graphs:
        return "unknown"
    metadata = (yaml.safe_load(graphs[0].read_text(encoding="utf-8")) or {}).get(
        "metadata"
    ) or {}
    topology = str(metadata.get("topology") or "")
    return "role_pool" if topology.startswith("template:") else "legacy_chain"


def _condition(run_dir: Path) -> str:
    """Engine plus the plan it executed.

    Same engine is not the same condition. One bplustree run replayed a legacy
    four-agent plan under the template builder while its batch-mates ran a
    five-agent plan that added a spec auditor and a gate repairer; grouping on
    the engine alone would have averaged those together.
    """
    shape: list[str] = []
    for path in sorted(Path(run_dir).glob("*/generated/graphs/*.yaml")):
        metadata = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get(
            "metadata"
        ) or {}
        roles = ",".join(
            str(agent.get("role")) for agent in metadata.get("agent_roster") or []
        )
        shape.append(f"{metadata.get('template_id') or metadata.get('topology')}({roles})")
    digest = sha1("|".join(shape).encode("utf-8")).hexdigest()[:7] if shape else "none"
    return f"{_engine(run_dir)}/{digest}"


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
            entry["engine"] = _engine(batch)
            entry["condition"] = _condition(batch)
            entry["agent_turns_run"] = _realized_turns(batch)
            if hidden_path.is_file():
                hidden = json.loads(hidden_path.read_text(encoding="utf-8"))
                for scored in hidden.get("results") or []:
                    if scored.get("task_id") != task:
                        continue
                    passed = int(scored.get("passed") or 0)
                    ceiling = _ceiling(task)
                    status = str(scored.get("status") or "")
                    entry.update(
                        {
                            "status": status,
                            # A run the harness never finished measuring is not a
                            # run that scored zero. Averaging it in would read as
                            # "this arm produced broken code" when the truth is
                            # "we have no measurement".
                            "measured": status not in {"timeout", "error"},
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
    ap.add_argument(
        "--engine",
        choices=("auto", "role_pool", "legacy_chain"),
        default="auto",
        help="report only runs from one subgraph builder",
    )
    args = ap.parse_args()

    runs = _collect(args.root)
    tasks = sorted({task for task, _ in runs})
    report: dict[str, dict] = {}

    header = (
        f"{'repo':<14}{'arm':<8}{'n':>2}{'scored':>7}  {'milestones':>10}  {'turns':>5}  "
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
            # A run with no hidden-eval file at all is as unmeasured as one that
            # timed out; counting it as scored overstates how much evidence the
            # arm actually rests on.
            scored_entries = [
                e
                for e in entries
                if e.get("measured", True) and e.get("pass_rate") is not None
            ]
            if args.engine != "auto":
                scored_entries = [
                    e for e in scored_entries if e.get("engine") == args.engine
                ]
            engines = sorted({e.get("condition", "unknown") for e in scored_entries})
            if len(engines) > 1:
                # Averaging across builders would report a code change as an
                # effect of the arm. Keep one and say which.
                #
                # Ties go to the newest builder rather than to whichever name
                # sorts first: a rerun that is half finished has equal counts,
                # and silently reporting the superseded half is the failure this
                # guard exists to prevent.
                kept = max(
                    engines,
                    key=lambda name: (
                        sum(1 for e in scored_entries if e.get("condition") == name),
                        _ENGINE_RECENCY.get(name.split("/")[0], 0),
                    ),
                )
                dropped = [e["batch"] for e in scored_entries if e.get("condition") != kept]
                scored_entries = [e for e in scored_entries if e.get("condition") == kept]
                print(
                    f"  ! {task}/{arm}: mixed conditions {engines}; scoring only "
                    f"{kept!r}, excluded {dropped}"
                )
            stats = {
                "n": len(entries),
                "n_scored": len(scored_entries),
                "unmeasured": [
                    e.get("batch") for e in entries if not e.get("measured", True)
                ],
                "conditions": sorted({e.get("condition", "unknown") for e in entries}),
                # Every column describes the same set of runs as the pass rate.
                # Averaging turns over runs that were excluded for being another
                # engine would describe a system nobody is scoring.
                "milestones": _mean([e.get("milestones") for e in scored_entries]),
                "agent_turns": _mean([e.get("agent_turns") for e in scored_entries]),
                "agent_turns_run": _mean(
                    [e.get("agent_turns_run") for e in scored_entries]
                ),
                "pass_rate_reachable": _mean(
                    [e.get("pass_rate_reachable") for e in scored_entries]
                ),
                "pass_rate": _mean([e.get("pass_rate") for e in scored_entries]),
                "committed": _mean([e.get("committed") for e in scored_entries]),
                "errors": [e["run_error"] for e in entries if e.get("run_error")],
                "runs": entries,
            }
            report[task][arm] = stats
            print(
                f"{task:<14}{arm:<8}{stats['n']:>2}{stats['n_scored']:>7}  "
                f"{stats['milestones']:>10.1f}  "
                f"{stats['agent_turns']:>5.1f}{stats['agent_turns_run']:>5.1f}  "
                f"{stats['pass_rate']:>9.3f}  "
                f"{stats['pass_rate_reachable']:>6.3f}  {stats['committed']:>9.1f}"
            )
        both = report[task]
        if "single" in both and "multi" in both:
            if not (both["single"]["n_scored"] and both["multi"]["n_scored"]):
                # Both arms print 0.000 when nothing was scored, and their
                # difference is a real-looking +0.000 that means "no data".
                print(f"{'':<14}{'delta':<8}{'':>2}  {'':>10}  {'':>5}  {'n/a':>9}")
                continue
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
