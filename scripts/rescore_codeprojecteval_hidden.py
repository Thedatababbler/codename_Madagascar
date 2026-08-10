#!/usr/bin/env python3
"""Recompute past held-out pass rates against the pinned suite sizes.

Runs before the pin existed divided by whatever the collect cache held at the
time, which for bplustree meant 59 in some runs and 356 in others. The pass
counts themselves are fine, so the record can be corrected without re-running
anything: recompute the rate from the stored `passed` and the pinned denominator.

Original values are kept; corrected ones are added alongside as `*_pinned`, so a
result can always be traced back to what was published at the time.
"""

from __future__ import annotations

import argparse
import json
import statistics as st
from collections import defaultdict
from pathlib import Path

from orchestra.codeprojecteval.suite_sizes import load_pinned_suite_sizes


def _arm(run_name: str) -> str:
    for arm in ("multi", "single", "solo", "planner"):
        if f"-{arm}-" in run_name:
            return arm
    return "unknown"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--outputs", type=Path, default=Path("outputs"))
    ap.add_argument("--write", action="store_true", help="add *_pinned keys in place")
    args = ap.parse_args()

    pinned = {t: sum(m.values()) for t, m in load_pinned_suite_sizes().items()}
    if not pinned:
        raise SystemExit("nothing pinned; run scripts/pin_codeprojecteval_suite_sizes.py")

    per: dict[tuple[str, str], list[tuple[float | None, float]]] = defaultdict(list)
    drifted: dict[str, set[int]] = defaultdict(set)
    files = sorted(args.outputs.rglob("hidden_eval.json"))
    for path in files:
        payload = json.loads(path.read_text(encoding="utf-8"))
        arm = _arm(path.parent.name)
        changed = False
        for result in payload.get("results") or []:
            task = result.get("task_id")
            passed = result.get("passed")
            if task is None or passed is None or task not in pinned:
                continue
            if result.get("tests_total"):
                drifted[task].add(int(result["tests_total"]))
            corrected = round(passed / pinned[task], 4)
            per[(task, arm)].append((result.get("pass_rate"), corrected))
            result["tests_total_pinned"] = pinned[task]
            result["pass_rate_pinned"] = corrected
            changed = True
        if changed and args.write:
            scored = [
                r["pass_rate_pinned"]
                for r in payload["results"]
                if r.get("pass_rate_pinned") is not None
            ]
            payload["mean_pass_rate_pinned"] = (
                round(st.mean(scored), 4) if scored else None
            )
            path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
            )

    print(f"{'任务':12s} {'臂':8s} {'n':>3s} {'原报告':>16s} {'钉住分母后':>16s} {'差':>7s}")
    for (task, arm), rows in sorted(per.items()):
        old = [o for o, _ in rows if o is not None]
        new = [n for _, n in rows]
        old_m = st.mean(old) if old else None
        new_m = st.mean(new)
        old_s = f"{old_m:.3f}±{st.stdev(old):.3f}" if len(old) > 1 else (
            f"{old_m:.3f}" if old_m is not None else "n/a"
        )
        new_s = f"{new_m:.3f}±{st.stdev(new):.3f}" if len(new) > 1 else f"{new_m:.3f}"
        delta = f"{new_m - old_m:+.3f}" if old_m is not None else "  n/a"
        print(f"{task:12s} {arm:8s} {len(rows):>3d} {old_s:>16s} {new_s:>16s} {delta:>7s}")

    bad = {t: sorted(d) for t, d in drifted.items() if len(d) > 1}
    if bad:
        print("\n分母曾漂过的任务(这是本次修复的原因):")
        for task, dens in sorted(bad.items()):
            print(f"  {task:12s} 见过 {dens} -> 钉为 {pinned[task]}")
    print(f"\n{len(files)} 个 hidden_eval.json{'已更新' if args.write else '(未写入,加 --write)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
