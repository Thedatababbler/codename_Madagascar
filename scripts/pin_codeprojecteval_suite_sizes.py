#!/usr/bin/env python3
"""Record how many test cases each held-out suite really collects.

Run once per dataset change. Counting happens against the reference
implementation, which is the only place the suite can be collected honestly, and
the result is committed so that every scoring run divides by the same number.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from orchestra.codeprojecteval.ceiling import collected_counts
from orchestra.codeprojecteval.dataset import DEFAULT_DATASET_ROOT, load_task
from orchestra.codeprojecteval.suite_sizes import (
    DEFAULT_PATH,
    load_pinned_suite_sizes,
    write_pinned_suite_sizes,
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("tasks", nargs="*", help="task ids; default: every pinned task")
    ap.add_argument("--dataset-root", type=Path, default=DEFAULT_DATASET_ROOT)
    ap.add_argument("--env-root", type=Path, default=Path("/root/codex-benchmarks/cpe_envs"))
    ap.add_argument("--out", type=Path, default=DEFAULT_PATH)
    args = ap.parse_args()

    pinned = load_pinned_suite_sizes(args.out)
    task_ids = args.tasks or sorted(pinned)
    if not task_ids:
        raise SystemExit("no tasks given and nothing pinned yet")

    updated = dict(pinned)
    for task_id in task_ids:
        python = args.env_root / task_id / "bin" / "python"
        if not python.is_file():
            print(f"{task_id:14s} SKIP  no environment at {python}")
            continue
        task = load_task(task_id, dataset_root=args.dataset_root)
        # Bypass the run-local cache: this script is the thing that decides.
        counts = collected_counts(task, python=python, cache_path=None)
        before = sum(pinned.get(task_id, {}).values())
        after = sum(counts.values())
        if not after:
            print(f"{task_id:14s} SKIP  collected nothing; leaving pin untouched")
            continue
        flag = "" if before in (0, after) else f"  CHANGED from {before}"
        print(f"{task_id:14s} {after:5d} cases in {len(counts):3d} modules{flag}")
        updated[task_id] = counts

    out = write_pinned_suite_sizes(updated, path=args.out)
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
