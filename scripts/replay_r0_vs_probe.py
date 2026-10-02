#!/usr/bin/env python3
"""Memory replay: how much does R0 (continue on the incumbent with evidence) gain over a probe (re-run as is)?

Reads the candidate ledger only; no model call. For every search that has
an incumbent, at least one probe and an R0 record with per-case results, it
compares each candidate with the incumbent on the incumbent's failing cases
(fixed = failed on the incumbent, passes here) and on the incumbent's passing
cases (regressed = passed on the incumbent, fails here). Probes are the
same-design resample control; R0 is the standard repair. The paired
difference per search is the number the whole procedure rests on.

    uv run python scripts/replay_r0_vs_probe.py [--ledger outputs/evolution/ledger] [--split train]
"""

from __future__ import annotations

import argparse
import statistics
from collections import defaultdict
from pathlib import Path

from orchestra.control.evolution.ledger import ledger_root_default, read_jsonl


def _cases(rec) -> dict[str, str]:
    return {k: v for k, v in (rec.get("per_case_results") or {}).items()}


def _delta(cand, incumbent) -> tuple[int, int, int] | None:
    ci, cc = _cases(incumbent), _cases(cand)
    if not ci or not cc:
        return None
    inc_fail = {k for k, v in ci.items() if v == "fail"}
    inc_pass = {k for k, v in ci.items() if v == "pass"}
    fixed = sum(1 for k in inc_fail if cc.get(k) == "pass")
    regressed = sum(1 for k in inc_pass if cc.get(k) == "fail")
    return fixed, regressed, fixed - regressed


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ledger", type=Path, default=None)
    ap.add_argument("--split", default="train")
    args = ap.parse_args()
    root = args.ledger or ledger_root_default()
    recs = []
    for f in sorted(root.glob("*/candidates.jsonl")):
        recs += read_jsonl(f)
    by_search: dict[tuple[str, str, str], list] = defaultdict(list)
    for r in recs:
        if args.split and str(r.get("split") or "train") != args.split:
            continue
        by_search[(str(r["task_id"]), str(r["milestone_id"]), str(r.get("run_dir") or ""))].append(r)

    rows = []
    for (task, mid, run), group in sorted(by_search.items()):
        inc = next((r for r in group if r.get("candidate_kind") in ("first_run", "incumbent")), None)
        probes = [r for r in group if r.get("candidate_kind") == "probe"]
        r0s = [r for r in group if r.get("candidate_kind") == "R0"]
        if inc is None or not probes or not r0s:
            continue
        d_r0 = _delta(r0s[0], inc)
        d_pr = [d for d in (_delta(p, inc) for p in probes) if d is not None]
        if d_r0 is None or not d_pr:
            continue
        best_probe = max(d_pr, key=lambda d: d[2])
        mean_probe = statistics.fmean(d[2] for d in d_pr)
        n_fail = sum(1 for v in _cases(inc).values() if v == "fail")
        rows.append({
            "task": task[3:] if task.startswith("rb_") else task, "milestone": mid, "rerun": "/reruns/" in run,
            "inc_score": inc.get("behaviour_score"), "inc_failing": n_fail,
            "r0_fixed": d_r0[0], "r0_regressed": d_r0[1], "r0_net": d_r0[2], "r0_score": r0s[0].get("behaviour_score"),
            "n_probes": len(d_pr), "probe_best_net": best_probe[2], "probe_mean_net": mean_probe,
            "probe_best_score": max(p.get("behaviour_score") or 0 for p in probes),
            "committed": next((r.get("candidate_kind") for r in group if r.get("committed")), None),
        })

    print(f"searches with incumbent + probes + R0 and per-case results: {len(rows)}\n")
    print(f"{'task':14s} {'milestone':30s} {'inc':>5s} {'fail':>4s} | {'R0 fix/reg/net':>14s} {'R0 sc':>5s} | {'probes':>6s} {'best net':>8s} {'mean net':>8s} {'best sc':>7s} | committed")
    for r in rows:
        tag = " (rerun)" if r["rerun"] else ""
        print(f"{r['task'][:14]:14s} {r['milestone'][:30]:30s} {r['inc_score']:5.2f} {r['inc_failing']:4d} | "
              f"{r['r0_fixed']:4d}/{r['r0_regressed']:3d}/{r['r0_net']:4d} {r['r0_score'] or 0:5.2f} | {r['n_probes']:6d} {r['probe_best_net']:8d} {r['probe_mean_net']:8.2f} {r['probe_best_score']:7.2f} | {r['committed']}{tag}")
    if not rows:
        return
    online = [r for r in rows if not r["rerun"]]
    for label, sub in (("all", rows), ("online only", online)):
        if not sub:
            continue
        r0 = [r["r0_net"] for r in sub]; pb = [r["probe_best_net"] for r in sub]; pm = [r["probe_mean_net"] for r in sub]
        diff_best = [a - b for a, b in zip(r0, pb)]; diff_mean = [a - b for a, b in zip(r0, pm)]
        wins = sum(1 for d in diff_best if d > 0); ties = sum(1 for d in diff_best if d == 0); losses = sum(1 for d in diff_best if d < 0)
        full = sum(1 for r in sub if r["r0_score"] == 1.0); full_p = sum(1 for r in sub if r["probe_best_score"] == 1.0)
        print(f"\n[{label}] n={len(sub)}")
        print(f"  R0 net fix per search:      mean {statistics.fmean(r0):.2f}  (incumbent failing cases mean {statistics.fmean(r['inc_failing'] for r in sub):.2f})")
        print(f"  best probe net fix:         mean {statistics.fmean(pb):.2f}")
        print(f"  mean probe net fix:         mean {statistics.fmean(pm):.2f}")
        print(f"  R0 minus best probe:        mean {statistics.fmean(diff_best):.2f}  wins/ties/losses {wins}/{ties}/{losses}")
        print(f"  R0 minus mean probe:        mean {statistics.fmean(diff_mean):.2f}")
        print(f"  reaches 1.0 on the suite:   R0 {full}/{len(sub)}, best probe {full_p}/{len(sub)}")
        print(f"  R0 regressed anything:      {sum(1 for r in sub if r['r0_regressed'] > 0)}/{len(sub)}")


if __name__ == "__main__":
    main()
