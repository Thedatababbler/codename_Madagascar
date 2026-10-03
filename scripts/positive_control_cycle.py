#!/usr/bin/env python3
"""Positive control for the design cycle's statistics (memory replay, no model call).

The ledger holds one comparison known to be large: R0 (continue on the
incumbent with evidence) beat the probes (re-run as is) 19/0/0. Two synthetic
rows are built from those very records and pushed through the cycle's
ranking and promotion rules:

* ``PC-P1``: "run a probe" as a trial row, R0 as its control. Every pair's
  delta is probe_net - R0_net on the incumbent's cases. The cycle must
  demote it (score <= 0 -> candidate, a second trial -> retired).
* ``PC-R1``: R0 as a trial row, the best probe as its control. The cycle
  must promote it (>= m pairs, score > 0, regressions not above the control).

If either verdict comes out wrong, the shrinkage strength or the promotion
threshold is mis-set, and that is worth knowing before the gate gets
stricter and real differences get smaller.

    uv run python scripts/positive_control_cycle.py [--ledger outputs/evolution/ledger] [--m 5] [--n0 3]
"""

from __future__ import annotations

import argparse
import statistics
from collections import defaultdict
from pathlib import Path

from orchestra.control.evolution.design_cycle import CycleConfig, update_row_states
from orchestra.control.evolution.ledger import ledger_root_default, read_jsonl
from orchestra.control.evolution.memory_replay import pair_statistics, ranking_from_stats
from orchestra.control.fast_loop.playbook_v2 import PlaybookRow


def _cases(rec) -> dict[str, str]:
    return dict(rec.get("per_case_results") or {})


def _net(cand, incumbent) -> tuple[int, int] | None:
    ci, cc = _cases(incumbent), _cases(cand)
    if not ci or not cc:
        return None
    fixed = sum(1 for k, v in ci.items() if v == "fail" and cc.get(k) == "pass")
    regressed = sum(1 for k, v in ci.items() if v == "pass" and cc.get(k) == "fail")
    return fixed - regressed, regressed


def synthetic_records(records: list[dict]) -> list[dict]:
    by_search: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for r in records:
        if str(r.get("split") or "train") != "train":
            continue
        by_search[(str(r["task_id"]), str(r["milestone_id"]), str(r.get("run_dir") or ""))].append(r)
    out: list[dict] = []
    for (task, mid, run), group in sorted(by_search.items()):
        inc = next((r for r in group if r.get("candidate_kind") in ("first_run", "incumbent")), None)
        probes = [r for r in group if r.get("candidate_kind") == "probe"]
        r0s = [r for r in group if r.get("candidate_kind") == "R0"]
        if inc is None or not probes or not r0s:
            continue
        r0n = _net(r0s[0], inc)
        pn = [(_net(p, inc), p) for p in probes]
        pn = [(n, p) for n, p in pn if n is not None]
        if r0n is None or not pn:
            continue
        best_net, best_probe = max(pn, key=lambda t: t[0][0])
        classes = list(r0s[0].get("error_classes") or inc.get("error_classes") or ["*"])
        base = {"task_id": task, "milestone_id": mid, "split": "train", "run_dir": run, "error_classes": classes,
                "candidate_kind": "row", "cost": {"usd": 0.1}, "prior_regressions": []}
        # control records (the pair partner), one per synthetic row
        out.append({**base, "record_id": f"{task}:{mid}:pc:ctrl_R0", "candidate_kind": "R0", "row_id": "",
                    "regressed": ["x"] * r0n[1], "net_fix": r0n[0]})
        out.append({**base, "record_id": f"{task}:{mid}:pc:ctrl_probe", "candidate_kind": "R0", "row_id": "",
                    "regressed": ["x"] * best_net[1], "net_fix": best_net[0]})
        # PC-P1: the probe as a row against R0
        out.append({**base, "record_id": f"{task}:{mid}:pc:PC-P1", "row_id": "PC-P1", "row_state_at_run": "trial",
                    "delta_vs_R0": best_net[0] - r0n[0], "paired_R0_record_id": f"{task}:{mid}:pc:ctrl_R0",
                    "regressed": ["x"] * best_net[1], "net_fix": best_net[0]})
        # PC-R1: R0 as a row against the best probe
        out.append({**base, "record_id": f"{task}:{mid}:pc:PC-R1", "row_id": "PC-R1", "row_state_at_run": "trial",
                    "delta_vs_R0": r0n[0] - best_net[0], "paired_R0_record_id": f"{task}:{mid}:pc:ctrl_probe",
                    "regressed": ["x"] * r0n[1], "net_fix": r0n[0]})
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ledger", type=Path, default=None)
    ap.add_argument("--m", type=int, default=5)
    ap.add_argument("--n0", type=int, default=3)
    args = ap.parse_args()
    root = args.ledger or ledger_root_default()
    records = []
    for f in sorted(root.glob("*/candidates.jsonl")):
        records += read_jsonl(f)
    synth = synthetic_records(records)
    n_pairs = sum(1 for r in synth if r.get("row_id") == "PC-P1")
    print(f"synthetic pairs from the ledger: {n_pairs}")
    stats = pair_statistics(synth, training_only=True)
    cfg = CycleConfig(m=args.m, n0=args.n0, max_trial_concurrent=0)
    for (row, cls), s in sorted(stats.items()):
        print(f"  {row} / {cls}: n={s.n} mean delta {statistics.fmean(s.deltas):+.2f} score {s.score(cfg.n0):+.2f} "
              f"regr {s.mean_regressions():.2f} vs control {s.mean_r0_regressions():.2f}")
    print("ranking:", ranking_from_stats(stats, n0=cfg.n0))
    rows = [
        PlaybookRow(row_id="PC-P1", table="repair", error_classes=("*",), action="S", instruction="probe", state="trial", intent="probe as a row"),
        PlaybookRow(row_id="PC-R1", table="repair", error_classes=("*",), action="S", instruction="r0", state="trial", intent="R0 as a row"),
    ]
    new_rows, changes = update_row_states(rows, stats, cfg=cfg)
    by = {r.row_id: r for r in new_rows}
    print("\nverdicts:")
    for c in changes:
        print(f"  {c.row_id}: {c.before} -> {c.after}  ({c.reason})")
    ok_p1 = by["PC-P1"].state in ("candidate", "retired")
    ok_r1 = by["PC-R1"].state == "active"
    # second trial of the demoted probe row: must retire
    again, changes2 = update_row_states([PlaybookRow(**{**by["PC-P1"].__dict__, "state": "trial"})], stats, cfg=cfg)
    ok_retire = again[0].state == "retired"
    print(f"\nPC-P1 (probe as row) demoted: {ok_p1} -> second trial retires: {ok_retire}")
    print(f"PC-R1 (R0 as row) promoted:   {ok_r1}")
    print("\nRESULT:", "PASS" if (ok_p1 and ok_r1 and ok_retire) else "FAIL")


if __name__ == "__main__":
    main()
