"""Pinned held-out suite sizes, so a score's denominator cannot drift.

The number of test cases in a task's held-out suite is a property of the dataset,
not of a run, but it used to be recomputed per run from a cache under ``outputs/``.
When that cache was absent the count fell back to counting ``def test_*``, which
undercounts a parametrised suite badly: bplustree was recorded with a denominator
of 59 in some runs and 356 in others, and pass rates of 2.54 and 3.12 followed.

Pinning the counts in a checked-in file makes the denominator the same for every
run, which is what lets two runs of the same task be compared at all.

Regenerate with ``scripts/pin_codeprojecteval_suite_sizes.py`` when the dataset
changes, and review the diff: a count that moves without the dataset moving means
collection is flaky, not that the suite grew.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DEFAULT_PATH = Path("configs/codeprojecteval_suite_sizes.json")


def load_pinned_suite_sizes(path: Path | None = None) -> dict[str, dict[str, int]]:
    """Per-task ``{module: collected_cases}``, or empty when nothing is pinned."""
    target = Path(path or DEFAULT_PATH)
    if not target.is_file():
        return {}
    payload: Any = json.loads(target.read_text(encoding="utf-8"))
    tasks = payload.get("tasks") if isinstance(payload, dict) else None
    if not isinstance(tasks, dict):
        return {}
    return {
        str(task_id): {str(k): int(v) for k, v in (modules or {}).items()}
        for task_id, modules in tasks.items()
        if isinstance(modules, dict)
    }


def pinned_total(task_id: str, path: Path | None = None) -> int:
    return sum(load_pinned_suite_sizes(path).get(task_id, {}).values())


def write_pinned_suite_sizes(
    tasks: dict[str, dict[str, int]], *, path: Path | None = None, note: str = ""
) -> Path:
    target = Path(path or DEFAULT_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "note": note
                or (
                    "Test cases pytest collects from each held-out module, counted "
                    "against the reference implementation. Denominators for scoring; "
                    "regenerate only when the dataset changes."
                ),
                "tasks": {
                    task_id: dict(sorted(modules.items()))
                    for task_id, modules in sorted(tasks.items())
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return target
