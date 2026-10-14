#!/usr/bin/env python3
"""Held-out per-case comparison of the sandboxed baseline with the old runs (reads the sealed script's outputs:
case ids, outcomes, milestones and failure categories only). Output: outputs/clean_baseline_cmp/case_cmp.json."""

from __future__ import annotations

import itertools
import json
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
D = ROOT / "outputs" / "clean_baseline_cmp"
TASKS = ["deprecated", "csvs-to-sqlite", "rsa", "djangorestframework-simplejwt", "voluptuous", "zxcvbn", "python-hl7"]


def main() -> int:
    out = {}
    for t in TASKS:
        d = json.loads((D / f"{t}.json").read_text())
        runs = d["runs"]
        new = next(b for b in runs if "cpe_sandbox_baseline" in b)
        evo = next(b for b in runs if "/cpe_evolution/" in b)
        olds = [b for b in runs if b != new and sum(v[0] == "pass" for v in runs[b].values()) > 0]   # a run that passed nothing failed to run
        ms = d["milestone_of"]
        lost = [c for c in d["cases"] if runs[evo][c][0] == "pass" and runs[new][c][0] != "pass"]
        gained = [c for c in d["cases"] if runs[new][c][0] == "pass" and runs[evo][c][0] != "pass"]
        by_ms = defaultdict(lambda: {"lost": 0, "gained": 0, "lost_categories": Counter()})
        for c in lost:
            by_ms[ms[c]]["lost"] += 1
            by_ms[ms[c]]["lost_categories"][runs[new][c][1] or "other"] += 1
        for c in gained:
            by_ms[ms[c]]["gained"] += 1
        # normal variation: share of cases whose outcome differs between two old runs, over all pairs of old runs
        pairs = list(itertools.combinations(olds, 2))
        disagree = [sum((runs[a][c][0] == "pass") != (runs[b][c][0] == "pass") for c in d["cases"]) / len(d["cases"]) for a, b in pairs]
        new_vs_old = [sum((runs[new][c][0] == "pass") != (runs[b][c][0] == "pass") for c in d["cases"]) / len(d["cases"]) for b in olds]
        # cases that every old run passed and the new run fails: the strongest signal of a change
        stable_lost = [c for c in lost if all(runs[b][c][0] == "pass" for b in olds)]
        out[t] = {"cases": len(d["cases"]), "pass": {b.split("/")[-1]: sum(v[0] == "pass" for v in runs[b].values()) for b in runs},
                  "lost_vs_evo": len(lost), "gained_vs_evo": len(gained),
                  "lost_categories": dict(Counter(runs[new][c][1] or "other" for c in lost)),
                  "by_milestone": {k: {"lost": v["lost"], "gained": v["gained"], "lost_categories": dict(v["lost_categories"])} for k, v in by_ms.items()},
                  "old_runs": [b.split("/")[-1] for b in olds],
                  "old_pairwise_disagreement": [round(x, 3) for x in disagree],
                  "new_vs_each_old_disagreement": [round(x, 3) for x in new_vs_old],
                  "lost_by_every_old_run_passed": len(stable_lost),
                  "stable_lost_by_milestone": dict(Counter(ms[c] for c in stable_lost)),
                  "stable_lost_categories": dict(Counter(runs[new][c][1] or "other" for c in stable_lost)),
                  "new_failures_categories": dict(Counter(v[1] or "other" for v in runs[new].values() if v[0] != "pass"))}
    (D / "case_cmp.json").write_text(json.dumps(out, indent=1))
    for t, r in out.items():
        print(t, "lost", r["lost_vs_evo"], "gained", r["gained_vs_evo"], "| old pairwise", r["old_pairwise_disagreement"],
              "| new vs olds", r["new_vs_each_old_disagreement"], "| lost though all old passed", r["lost_by_every_old_run_passed"])
        print("    by milestone", {k: (v["lost"], v["gained"], v["lost_categories"]) for k, v in r["by_milestone"].items()})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
