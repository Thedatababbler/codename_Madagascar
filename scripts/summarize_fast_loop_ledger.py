#!/usr/bin/env python3
"""The table-evolution ring's eyes: aggregate every search's records per playbook.

Reads each run's ``task_execution.json`` and reports, per playbook row, the
paired within-run evidence the tables are admitted and removed on: the delta
against the same search's anchor samples (never a cross-run score, whose
yardstick moves with the re-authored suite), persistent failures fixed,
regressions introduced against the incumbent, cost, and whether the row's
declared intent is being met. "Which candidate won" is deliberately absent:
at one sample per design that is mostly the resampling lottery.

    uv run python scripts/summarize_fast_loop_ledger.py [--glob PATTERN] [--out FILE]
"""

from __future__ import annotations

import argparse
import glob
import json
from collections import defaultdict
from pathlib import Path

from orchestra.control.fast_loop.persistence import failure_key
from orchestra.control.fast_loop.playbooks import CATALOG, QUALITY_CATALOG

INTENTS = {p.playbook_id: p.intent for p in (*CATALOG, *QUALITY_CATALOG)}


def _keys(names: list[str] | None) -> set[str]:
    return {failure_key(n) for n in (names or [])}


def collect(pattern: str) -> tuple[list[dict], int]:
    rows: list[dict] = []
    searches = 0
    for path in sorted(glob.glob(pattern)):
        parts = Path(path).parents
        run = f"{parts[4].name.split('_', 2)[-1]}/{parts[3].name.replace('cpe-2026', '')}"
        data = json.loads(Path(path).read_text())
        for milestone, st in (data.get("fast_loop_states") or {}).items():
            cands = st.get("candidates") or []
            anchors = [
                c
                for c in cands
                if not c.get("playbook_id")
                and str(c.get("candidate_id", "")).startswith("cand_feedback")
                and c.get("behaviour_score") is not None
            ]
            incumbent = next(
                (c for c in cands if c.get("candidate_id") == "incumbent_first_pass"),
                None,
            )
            played = [c for c in cands if c.get("playbook_id")]
            if not played:
                continue
            searches += 1
            anchor_scores = [c["behaviour_score"] for c in anchors]
            inc_fail = _keys(incumbent.get("behaviour_failures")) if incumbent else None
            for c in played:
                meta = c.get("metadata") or {}
                ledger = meta.get("persistence_ledger") or {}
                score = c.get("behaviour_score")
                own = _keys(c.get("behaviour_failures"))
                rows.append(
                    {
                        "run": run,
                        "milestone": milestone,
                        "playbook_id": c["playbook_id"],
                        "role": meta.get("recommended_role") or "",
                        "role_source": meta.get("role_source") or "",
                        "failure_class": meta.get("failure_class") or "",
                        "status": str(c.get("status") or ""),
                        "gate_ok": (c.get("quality_score") or 0) >= 1.0,
                        "score": score,
                        "d_mean": (
                            round(score - sum(anchor_scores) / len(anchor_scores), 4)
                            if score is not None and anchor_scores
                            else None
                        ),
                        "d_best": (
                            round(score - max(anchor_scores), 4)
                            if score is not None and anchor_scores
                            else None
                        ),
                        "pfix": ledger.get("persistent_fixed_count"),
                        "ptot": ledger.get("persistent_total"),
                        "regressions": (
                            len(own - inc_fail)
                            if inc_fail is not None and score is not None
                            else None
                        ),
                        "cost": (c.get("cost") or {}).get("estimated_cost_usd"),
                    }
                )
    return rows, searches


def render(rows: list[dict], searches: int) -> str:
    by_pb: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_pb[r["playbook_id"]].append(r)
    lines = [
        f"# Fast-loop ledger — {len(rows)} playbook appearances over {searches} searches",
        "",
        "Paired, within-run only. d_best>0 means the row beat the best anchor",
        "sample in its own search — the bar a slot has to clear to out-earn",
        "resampling. pfix/ptot is persistent failures fixed (persistence arm only).",
        "",
        "| playbook | n | gate ok | d_best>0 | mean d_best | mean d_mean | pfix/ptot | mean regr | mean $ | intent |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for pb in sorted(by_pb):
        rs = by_pb[pb]
        scored = [r for r in rs if r["d_best"] is not None]
        pf = [r for r in rs if r["pfix"] is not None]
        regs = [r["regressions"] for r in rs if r["regressions"] is not None]
        def mean(vals):
            return round(sum(vals) / len(vals), 3) if vals else None
        lines.append(
            "| {pb} | {n} | {gate} | {wins}/{ns} | {db} | {dm} | {pfix} | {regr} | {cost} | {intent} |".format(
                pb=pb,
                n=len(rs),
                gate=sum(1 for r in rs if r["gate_ok"]),
                wins=sum(1 for r in scored if r["d_best"] > 0),
                ns=len(scored),
                db=mean([r["d_best"] for r in scored]),
                dm=mean([r["d_mean"] for r in scored]),
                pfix=(
                    f"{sum(r['pfix'] for r in pf)}/{sum(r['ptot'] for r in pf)}"
                    if pf
                    else "—"
                ),
                regr=mean(regs),
                cost=mean([r["cost"] for r in rs if r["cost"]]),
                intent=(INTENTS.get(pb) or "")[:52],
            )
        )
    lines += ["", "## Per-appearance detail", ""]
    lines.append("| run | milestone | playbook | status | score | d_best | pfix | regr | role(src) |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        pfix = f"{r['pfix']}/{r['ptot']}" if r["pfix"] is not None else "—"
        score = round(r["score"], 4) if r["score"] is not None else "—"
        lines.append(
            f"| {r['run']} | {r['milestone'][:28]} | {r['playbook_id']} | {r['status']} | "
            f"{score} | {r['d_best'] if r['d_best'] is not None else '—'} | {pfix} | "
            f"{r['regressions'] if r['regressions'] is not None else '—'} | {r['role']}({r['role_source']}) |"
        )
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--glob",
        default="outputs/cpe_official_*/cpe-*/*/tasks/*/task_execution.json",
    )
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    rows, searches = collect(args.glob)
    text = render(rows, searches)
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n")


if __name__ == "__main__":
    main()
