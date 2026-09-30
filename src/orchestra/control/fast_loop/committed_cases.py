"""What a committed milestone's own frozen suite said, case by case.

Written beside the frozen suite when the milestone commits, read by later
milestones' searches: a case that passed here and fails on a candidate of a
later milestone is a regression *by* that candidate, and the unified
acceptance rule refuses it (adamas_milestone_self_evolution_prompt.md §2.5).
The sidecar is the only per-case record that survives the run summary, whose
stages keep counts and failed names but not the passing names.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from orchestra.control.fast_loop.persistence import failure_key

COMMITTED_CASES_SUFFIX = ".committed_cases.json"


def committed_cases_path(spec_dir: str | Path) -> Path:
    spec = Path(spec_dir)
    return spec.parent / (spec.name + COMMITTED_CASES_SUFFIX)


def write_committed_cases(
    spec_dir: str | Path | None,
    *,
    passed: list[str],
    failed: list[str],
    source: str,
    milestone_id: str = "",
) -> Path | None:
    """Record the per-case verdicts of the result being committed. Never raises."""
    if not spec_dir:
        return None
    path = committed_cases_path(spec_dir)
    payload: dict[str, Any] = {
        "milestone_id": milestone_id,
        "source": source,
        "passed": sorted({failure_key(n) for n in passed}),
        "failed": sorted({failure_key(n) for n in failed}),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError:
        return None
    return path


def read_committed_cases(spec_dir: str | Path | None) -> dict[str, Any] | None:
    if not spec_dir:
        return None
    path = committed_cases_path(spec_dir)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return {
        "milestone_id": str(data.get("milestone_id") or ""),
        "source": str(data.get("source") or ""),
        "passed": [str(x) for x in (data.get("passed") or [])],
        "failed": [str(x) for x in (data.get("failed") or [])],
    }


__all__ = ["COMMITTED_CASES_SUFFIX", "committed_cases_path", "read_committed_cases", "write_committed_cases"]
