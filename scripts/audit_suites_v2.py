#!/usr/bin/env python3
"""Run the §2.4 mechanical audit over frozen suites, with the documents and the
documented public symbols of the milestone (lenient set: own + predecessors +
unattributed). Reads documents and suite source only.

    uv run python scripts/audit_suites_v2.py <suite_dir> [...]   # task/milestone from the path
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from orchestra.codeprojecteval.public_symbols import derive_public_symbols, load_docs
from orchestra.codeprojecteval.suite_audit import audit_suite

DATASET_ROOT = Path(os.environ.get("CPE_DATASET_ROOT") or "/root/codex-benchmarks/projectgen/datasets/CodeProjectEval/python-subset")
PLANS = Path("configs/datasets/cpe_feature_plans")
TASKS = sorted((p.name for p in DATASET_ROOT.iterdir() if p.is_dir()), key=len, reverse=True)


def task_and_milestone(suite: Path) -> tuple[str, str]:
    s = str(suite)
    task = next((t for t in TASKS if f"/{t}/" in s or f"-{t}-" in s), "")
    m = re.search(r"/([\w-]+)\.spec_tests/?$", s)
    return task, (m.group(1) if m else "")


def packages_of(task: str) -> list[str]:
    cfg = json.loads((DATASET_ROOT / task / "config.json").read_text(encoding="utf-8"))
    pk = cfg.get("packages") or cfg.get("package") or []
    if isinstance(pk, str):
        pk = [pk]
    if not pk:
        pk = [p.name for p in (DATASET_ROOT / task).iterdir() if p.is_dir() and (p / "__init__.py").exists()]
    return [str(p) for p in pk]


def allowed_for(task: str, milestone: str):
    inv = derive_public_symbols(load_docs(DATASET_ROOT / task / "docs"))
    plan = json.loads((PLANS / f"{task}.plan.json").read_text(encoding="utf-8"))
    ms = {m["milestone_id"]: m for m in plan["milestones"]}
    own = ms.get(milestone, {}).get("focus_paths") or []
    # predecessors: every milestone earlier in plan order (the user's rule), plus declared dependencies
    order = [m["milestone_id"] for m in plan["milestones"]]
    idx = order.index(milestone) if milestone in order else len(order)
    pred_ids = set(order[:idx]) | set(ms.get(milestone, {}).get("depends_on") or [])
    pred = [fp for d in pred_ids if d in ms for fp in (ms[d].get("focus_paths") or [])]
    return inv.lenient(own, pred), inv


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("suites", nargs="+")
    ap.add_argument("--no-symbols", action="store_true", help="skip the public-symbol rule")
    ap.add_argument("--json", type=Path, default=None)
    args = ap.parse_args()
    rows = []
    for s in args.suites:
        suite = Path(s)
        task, milestone = task_and_milestone(suite)
        if not task:
            print(f"skip {suite}: task not recognised", file=sys.stderr)
            continue
        docs = load_docs(DATASET_ROOT / task / "docs")
        allowed = None if args.no_symbols else allowed_for(task, milestone)[0]
        r = audit_suite(suite, docs, packages=packages_of(task), allowed_symbols=allowed)
        rows.append({"suite": str(suite), "task": task, "milestone": milestone, **r.to_dict()})
        print(f"{task:14s} {milestone:42s} cases {r.cases:3d} cited {r.cited_ratio:.2f} soft {len(r.soft_cases):2d} violations {len(r.violations):3d} {r.by_rule()}")
        for v in r.violations[:6]:
            print(f"      [{v.rule}] {v.file}::{v.case} L{v.line}: {v.detail[:110]}")
    if args.json:
        args.json.write_text(json.dumps(rows, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
