#!/usr/bin/env python3
"""Aggregate author-probe audits by label: one row per label, one per (label, milestone) on request.

    uv run python scripts/author_probe_compare.py outputs/author_probe/*.audit.json [--by-milestone]
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

SUM = ["cases", "priming_tests", "priming_fixtures", "shim_total", "private_access_tests", "broad_raises",
       "module_top_project_imports", "ref_collection_errors", "ref_fail_unsupported"]
MEAN = ["cited_ratio", "ref_validity"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+", type=Path)
    ap.add_argument("--by-milestone", action="store_true")
    ap.add_argument("--labels", default=None, help="comma list, in display order")
    args = ap.parse_args()
    rows = [r for f in args.files for r in json.loads(f.read_text())]
    labels = args.labels.split(",") if args.labels else sorted({r["label"] for r in rows})

    def agg(group: list[dict]) -> dict:
        out = {"suites": len(group)}
        for k in SUM:
            out[k] = sum(int(r.get(k) or 0) for r in group)
        for k in MEAN:
            vals = [r[k] for r in group if r.get(k) is not None]
            out[k] = round(sum(vals) / len(vals), 2) if vals else None
        out["suites_with_priming"] = sum(1 for r in group if (r.get("priming_tests") or r.get("priming_fixtures")))
        out["suites_with_shims"] = sum(1 for r in group if r.get("shim_total"))
        out["suites_with_private"] = sum(1 for r in group if r.get("private_access_tests"))
        return out

    cols = ["suites", "cases", "cited_ratio", "priming_tests", "suites_with_priming", "shim_total", "suites_with_shims",
            "private_access_tests", "suites_with_private", "broad_raises", "module_top_project_imports",
            "ref_validity", "ref_collection_errors", "ref_fail_unsupported"]
    print("label".ljust(16) + " | " + " | ".join(cols))
    for label in labels:
        a = agg([r for r in rows if r["label"] == label])
        print(label.ljust(16) + " | " + " | ".join(str(a[c]) for c in cols))
    if args.by_milestone:
        print()
        by = defaultdict(list)
        for r in rows:
            by[(r["task"], r["milestone"])].append(r)
        keys = ["cases", "cited_ratio", "priming_tests", "shim_total", "private_access_tests", "ref_validity", "ref_fail_unsupported"]
        for (task, ms), group in sorted(by.items()):
            print(f"{task}:{ms}")
            for r in sorted(group, key=lambda r: labels.index(r["label"]) if r["label"] in labels else 99):
                print("   " + r["label"].ljust(16) + " " + "  ".join(f"{k}={r.get(k)}" for k in keys))


if __name__ == "__main__":
    main()
