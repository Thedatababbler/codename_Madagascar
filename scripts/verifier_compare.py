#!/usr/bin/env python3
"""Rows, R0 and probes measured on an independent verification suite (memory replay).

Reads the per-workspace suite results `scripts/author_replay.py score` wrote
for a verifier suite (one JSON per milestone run, every retained workspace
of the milestone scored) and the ledger (to know which workspace was the
incumbent, a probe, R0, a row, a resample). For each search it reports, on
the verifier's cases that the dataset reference passes: the incumbent's
failures, and for every other candidate the net change against the
incumbent (incumbent-fail -> pass, minus incumbent-pass -> fail). This is
the yardstick the gate suite cannot be: nobody repaired against it.

    uv run python scripts/verifier_compare.py outputs/evolution/author_replay/verify2/*.json
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
from pathlib import Path

from orchestra.control.evolution.ledger import ledger_root_default, read_jsonl


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--ledger", type=Path, default=None)
    args = ap.parse_args()
    root = args.ledger or ledger_root_default()
    kinds: dict[str, tuple[str, str]] = {}
    for f in sorted(root.glob("*/candidates.jsonl")):
        for r in read_jsonl(f):
            ws = str(r.get("workspace_ref") or "")
            if ws:
                kinds[ws.rstrip("/")] = (str(r.get("candidate_kind") or ""), str(r.get("row_id") or ""))
    rows = []
    agg: dict[str, list[int]] = defaultdict(list)
    for f in args.files:
        d = json.loads(Path(f).read_text(encoding="utf-8"))
        ws = {w["workspace"]: w for w in d["workspaces"]}
        inc = ws.get("first_pass")
        if inc is None or not inc.get("suite_total"):
            continue
        inc_fail = set(inc["suite_failed"])
        line = {"task": d["task"], "milestone": d["milestone"], "verifier_cases": inc["suite_total"], "incumbent_fails": len(inc_fail), "candidates": []}
        for name, w in ws.items():
            if name == "first_pass" or not w.get("suite_total"):
                continue
            kind, row = kinds.get(w["path"].rstrip("/"), ("?", ""))
            if kind == "?":
                # candidate workspaces the ledger did not reference by path: the directory is the candidate id
                n = name
                if n == "final":
                    kind = "final"  # the run's committed repository (the winner, after later milestones)
                elif n.startswith("cand_feedback"):
                    kind = "probe"
                elif n == "cand_R0":
                    kind = "R0"
                elif n.startswith("cand_node_resample"):
                    kind = "resample"
                elif n.startswith("cand_v2:") or n.startswith("cand_pb_"):
                    kind, row = "row", n.split("cand_", 1)[-1].replace("v2:", "")
            fail = set(w["suite_failed"])
            fixed = len(inc_fail - fail)
            regressed = len(fail - inc_fail)
            net = fixed - regressed
            label = {"R0": "R0", "probe": "probe", "row": f"row:{row.split('~')[0]}", "resample": "resample"}.get(kind, kind)
            line["candidates"].append((label, fixed, regressed, net, len(fail)))
            agg[label.split(":")[0]].append(net)
        rows.append(line)
    for line in rows:
        print(f"\n{line['task']} / {line['milestone']}  verifier cases {line['verifier_cases']}, incumbent fails {line['incumbent_fails']}")
        for label, fixed, regressed, net, left in sorted(line["candidates"], key=lambda c: (c[0] != "R0", c[0])):
            print(f"   {label:22s} fixed {fixed:2d} regressed {regressed:2d} net {net:+3d}  (fails left {left})")
    print("\nnet change against the incumbent on the verifier, by candidate kind:")
    for kind, nets in sorted(agg.items()):
        print(f"   {kind:10s} n={len(nets):2d} mean {statistics.fmean(nets):+.2f}  wins {sum(1 for n in nets if n > 0)} ties {sum(1 for n in nets if n == 0)} losses {sum(1 for n in nets if n < 0)}")
    # paired: row vs R0 and probe vs R0 within the same search
    pairs = defaultdict(list)
    for line in rows:
        r0 = [c for c in line["candidates"] if c[0] == "R0"]
        if not r0:
            continue
        for label, *_rest, net, _left in [(c[0], c[1], c[2], c[3], c[4]) for c in line["candidates"] if c[0] != "R0"]:
            pairs[label.split(":")[0]].append(net - r0[0][3])
        for c in line["candidates"]:
            if c[0] == "probe":
                pass
    print("\npaired against R0 in the same search (candidate net minus R0 net on the verifier):")
    for kind, ds in sorted(pairs.items()):
        print(f"   {kind:10s} n={len(ds):2d} mean {statistics.fmean(ds):+.2f}  better {sum(1 for d in ds if d > 0)} same {sum(1 for d in ds if d == 0)} worse {sum(1 for d in ds if d < 0)}")


if __name__ == "__main__":
    main()
