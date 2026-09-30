#!/usr/bin/env python3
"""Attribute held-out cases to milestones (self-evolution spec §5.4), by symbols only.

For one task: every ``unit_tests`` case is mapped to the milestone whose focus
paths contain the module the case imports or the public symbol it calls.
The output is a JSON file of case-id sets per milestone and nothing else --
no pass / fail, no source. It is the only place outside
``scripts/eval_codeprojecteval.py`` that opens the held-out directory, and
it never runs it.

    uv run python scripts/heldout_attribution.py --task tinydb --plan configs/datasets/cpe_feature_plans/tinydb.plan.json \\
        --out outputs/evolution/sealed/attribution/tinydb.json
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
from collections import defaultdict
from pathlib import Path

DATASET_ROOT = Path(os.environ.get("CPE_DATASET_ROOT") or "/root/codex-benchmarks/projectgen/datasets/CodeProjectEval/python-subset")


def _stems(paths: list[str]) -> set[str]:
    out = set()
    for p in paths:
        name = str(p).rstrip("/").rsplit("/", 1)[-1]
        stem = name[:-3] if name.endswith(".py") else name
        if stem and stem != "__init__":
            out.add(stem)
        parts = [x for x in str(p).replace(".py", "").split("/") if x and x not in ("src",)]
        if parts:
            out.add(".".join(parts))
    return out


def _case_symbols(tree: ast.AST) -> dict[str, set[str]]:
    """test id -> module stems and attribute heads it refers to."""
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                imports.add(a.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
            for a in node.names:
                imports.add(f"{node.module}.{a.name}")
    out: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            names = set(imports)
            for sub in ast.walk(node):
                if isinstance(sub, ast.Name):
                    names.add(sub.id)
                elif isinstance(sub, ast.Attribute):
                    names.add(sub.attr)
            out[node.name] = names
    return out


def attribute(task: str, plan_path: Path) -> dict[str, list[str]]:
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    focus = {m["milestone_id"]: _stems(m.get("focus_paths") or []) for m in plan["milestones"]}
    cfg = json.loads((DATASET_ROOT / task / "config.json").read_text(encoding="utf-8"))
    tests_dir = DATASET_ROOT / task / str(cfg.get("unit_tests") or "unit_tests")
    result: dict[str, list[str]] = defaultdict(list)
    for path in sorted(tests_dir.rglob("test_*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError):
            continue
        rel = str(path.relative_to(tests_dir))
        for test_name, names in _case_symbols(tree).items():
            best, best_hits = "", 0
            for mid, stems in focus.items():
                hits = sum(1 for n in names if n in stems or any(n.startswith(s + ".") or n.endswith("." + s) for s in stems))
                if hits > best_hits:
                    best, best_hits = mid, hits
            if best:
                result[best].append(f"{rel}::{test_name}")
    return {mid: sorted(cases) for mid, cases in result.items()}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True)
    ap.add_argument("--plan", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args()
    data = attribute(args.task, args.plan)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"task_id": args.task, "attribution": data}, indent=2), encoding="utf-8")
    print("wrote", args.out, {k: len(v) for k, v in data.items()})


if __name__ == "__main__":
    main()
