#!/usr/bin/env python3
"""Report how much of each held-out suite the design documents can specify.

Run this before attributing any held-out score to decomposition: a repository
whose tests import names the specification never states has a ceiling below 1.0
for every system, however it is built.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from orchestra.codeprojecteval.ceiling import analyze_ceiling
from orchestra.codeprojecteval.dataset import DEFAULT_DATASET_ROOT, load_task


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", type=str, default="")
    ap.add_argument("--dataset-root", type=Path, default=DEFAULT_DATASET_ROOT)
    ap.add_argument("--out", type=Path, default=Path("outputs/cpe_ceiling.json"))
    args = ap.parse_args()

    from orchestra.cli.run_codeprojecteval_decomp import USABLE_TASKS

    names = [n.strip() for n in args.tasks.split(",") if n.strip()] or list(
        USABLE_TASKS
    )
    reports = [
        analyze_ceiling(load_task(name, dataset_root=args.dataset_root))
        for name in names
    ]
    for report in reports:
        print(
            f"{report.task_id:24s} tests={report.tests_total:4d} "
            f"blocked={report.tests_blocked:4d} "
            f"ceiling={report.reachable_ceiling:.3f}  "
            f"missing={', '.join(report.undocumented_names[:6])}"
        )

    clean = [r.task_id for r in reports if r.reachable_ceiling >= 0.99]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(
            {"results": [r.to_dict() for r in reports], "clean_ceiling": clean},
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    mean = sum(r.reachable_ceiling for r in reports) / len(reports)
    print(f"\nmean ceiling={mean:.3f}; ceiling>=0.99: {', '.join(clean)}")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
