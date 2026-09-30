#!/usr/bin/env python3
"""Error-class distribution of the persistent failures already on record (§8.1).

Reads every run's ``task_execution.json``, takes each searched milestone's
persistent set (``fast_loop_states[*].persistence.persistent``), classifies
the cases with the stage-2 rules from the frozen suite's source (no outputs
are re-run, so the output-based rules -- timeouts, DID NOT RAISE -- do not
fire here), and reports how many samples each class has. Classes below
``--min`` are flagged with the merge the spec suggests (E6 -> E3, E8 -> E1),
for a human to confirm in the config.

    uv run python scripts/error_class_distribution.py --glob 'outputs/cpe_milestones/*/*/tasks/*/task_execution.json' \\
        --out docs/reports/error_class_distribution.md
"""

from __future__ import annotations

import argparse
import glob
import json
from collections import Counter, defaultdict
from pathlib import Path

from orchestra.control.fast_loop import node_resample as nr
from orchestra.control.fast_loop.controller import _test_source
from orchestra.control.fast_loop.error_classes import CaseFacts, classify_persistent
from orchestra.control.fast_loop.persistence import failure_key

MERGE = {"E6": "E3", "E8": "E1", "E4": "E3", "E7": "E3"}


def _frozen(harness_dir: Path, milestone: str) -> Path | None:
    p = harness_dir / f"{milestone}.spec_tests"
    return p if p.is_dir() else None


def _facts(frozen: Path, repo: Path | None, case: str) -> CaseFacts:
    key = failure_key(case)
    name = key.split("::")[-1]
    file_part, _, tid = key.partition("::")
    path = frozen / file_part
    source = _test_source(path, tid) if path.is_file() else ""
    files: set[str] = set()
    if repo is not None and repo.is_dir() and path.is_file():
        index = nr.repo_symbol_index(repo)
        files = {index[n] for n in nr.symbols_in_test(path, tid) if n in index}
    return CaseFacts(case_id=case, key=key, name=name, source=source, symbol_files=files)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--glob", default="outputs/cpe_milestones/*/*/tasks/*/task_execution.json")
    ap.add_argument("--min", type=int, default=5)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    totals: Counter[str] = Counter()
    per_milestone: list[tuple[str, str, tuple[str, ...], dict[str, int]]] = []
    for path in sorted(glob.glob(args.glob)):
        p = Path(path)
        task_dir = p.parents[2]
        harness = task_dir / "harness"
        try:
            data = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        for milestone, st in (data.get("fast_loop_states") or {}).items():
            persistent = list((st.get("persistence") or {}).get("persistent") or [])
            if not persistent:
                continue
            frozen = _frozen(harness, milestone)
            if frozen is None:
                continue
            repo = task_dir / "tasks" / f"rb_{task_dir.name}" / "canonical" / "repo"
            facts = [_facts(frozen, repo if repo.is_dir() else None, c) for c in persistent]
            cls = classify_persistent(facts)
            totals.update(cls.by_case.values())
            per_milestone.append((task_dir.parent.name, milestone, cls.classes, dict(cls.counts)))

    lines = ["# Error-class distribution of recorded persistent failures", "",
             f"Runs scanned: `{args.glob}`; {len(per_milestone)} searched milestones with a persistent set.", "",
             "| class | persistent cases | milestones where it is the main class |", "|---|---|---|"]
    main_counts: Counter[str] = Counter()
    for _, _, classes, _ in per_milestone:
        for c in classes:
            main_counts[c] += 1
    for c in ("E1", "E2", "E3", "E4", "E5", "E6", "E7", "E8", "E9"):
        lines.append(f"| {c} | {totals.get(c, 0)} | {main_counts.get(c, 0)} |")
    thin = [c for c in ("E1", "E2", "E3", "E4", "E5", "E6", "E7", "E8") if totals.get(c, 0) < args.min]
    lines += ["", f"Classes with fewer than {args.min} cases: " + (", ".join(thin) if thin else "none") + "."]
    if thin:
        lines.append("Suggested merges (confirm by hand before writing them into the config): "
                     + "; ".join(f"{c} -> {MERGE.get(c, 'E3')}" for c in thin) + ".")
    lines += ["", "Rules that need the failure output (timeouts, DID NOT RAISE) did not fire in this offline pass;",
              "E5 was reached from `pytest.raises` in the source alone, E8 not at all.", "",
              "| run | milestone | main classes | counts |", "|---|---|---|---|"]
    for run, ms, classes, counts in per_milestone:
        lines.append(f"| {run} | {ms} | {', '.join(classes) or '-'} | {counts} |")
    text = "\n".join(lines) + "\n"
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="utf-8")
        print("wrote", args.out)
    print("\n".join(lines[:16]))


if __name__ == "__main__":
    main()
