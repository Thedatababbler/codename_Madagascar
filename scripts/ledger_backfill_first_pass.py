#!/usr/bin/env python3
"""Add ledger records for milestones that never entered the fast loop (self-evolution spec §5).

The controller writes the ledger when a search runs. A milestone that
commits on its first pass, or fails before any search, leaves only
``task_execution.json`` behind. This script turns those into one milestone
record and one ``incumbent`` candidate record each (per-case results from
the gate's spec_tests stage, first-pass decision joined in), so the first-pass
calibration and the F-entry pairing (§7.3, §8.3) see every milestone.
Idempotent: a milestone already in the ledger with the same final status is
skipped. Cost fields are left at zero and flagged ``cost_quality: unknown``.

    uv run python scripts/ledger_backfill_first_pass.py outputs/cpe_evolution/<batch>/<task> [...]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from orchestra.control.evolution.ledger import (
    append_jsonl,
    ledger_root_default,
    read_jsonl,
    split_of,
)
from orchestra.control.fast_loop.playbook_v2 import table_version


def _case_name(node_id: str) -> str:
    # '../../harness/<ms>.spec_tests/test_x.py::test_y' -> 'test_x.py::test_y'
    return node_id.split(".spec_tests/", 1)[-1]


def records_for_run(run_dir: Path, *, existing: set[tuple[str, str, str]], version: str) -> tuple[list[dict], list[dict]]:
    files = sorted(run_dir.glob("tasks/rb_*/task_execution.json"))
    if not files:
        return [], []
    t = json.loads(files[0].read_text(encoding="utf-8"))
    task_id = str(t.get("task_id") or "")
    split = split_of(task_id.removeprefix("rb_"))
    decisions = {}
    fp = run_dir / "first_pass_decision.json"
    if fp.is_file():
        for d in json.loads(fp.read_text(encoding="utf-8")):
            decisions[str(d.get("milestone_id"))] = d
    cands, miles = [], []
    for mid, sub in (t.get("subtasks") or {}).items():
        status = str(sub.get("status") or "")
        if status in ("pending", "ready", "running") or (task_id, mid, status) in existing:
            continue
        attempts = sub.get("attempts") or []
        last = attempts[-1] if attempts else {}
        meta = last.get("metadata") or {}
        stages = {s.get("stage"): s for s in (meta.get("harness_stages") or [])}
        spec = stages.get("spec_tests") or {}
        passed = [_case_name(x) for x in (spec.get("passed_tests") or [])]
        failed = [_case_name(x) for x in (spec.get("failed_tests") or [])]
        per_case = {**{c: "pass" for c in passed}, **{c: "fail" for c in failed}}
        dec = decisions.get(mid) or {}
        features = dict(dec.get("features") or {})
        applied = [str(x) for x in (dec.get("applied") or [])]
        score = meta.get("behaviour_score")
        rec_id = f"{task_id}:{mid}:incumbent_first_pass"
        if status == "committed" and any(r[0] == task_id and r[1] == mid for r in existing):
            rec_id += "_resumed"
        cands.append({
            "record_id": rec_id, "task_id": task_id, "milestone_id": mid, "split": split,
            "playbook_version": version, "first_pass_version": "F0", "suite_version": "",
            "features": features, "f_entries_applied": applied, "assignment": str(dec.get("assignment") or "deterministic"),
            "predicted_error_classes": list(dec.get("predicted_error_classes") or []),
            "actual_error_classes": {}, "error_classes": [], "candidate_kind": "incumbent", "row_id": "",
            "row_state_at_run": "", "status": status, "behaviour_score": score, "per_case_results": per_case,
            "persistent_before": failed, "flaky_before": [], "stable_pass_before": passed, "fixed": [], "regressed": [],
            "prior_regressions": [], "net_fix": None, "accepted": status == "committed", "paired_R0_record_id": "",
            "delta_vs_R0": None, "committed": status == "committed", "filtered_reason": "",
            "cost": {"calls": 0, "wall_seconds": 0.0, "tokens": 0, "usd": 0.0, "cost_quality": "unknown"},
            "workspace_ref": str(sub.get("workspace_ref") or ""), "patch_hash": "", "llm_calls": [], "run_dir": str(run_dir),
            "source": "backfill",
        })
        miles.append({
            "task_id": task_id, "milestone_id": mid, "split": split, "features": features, "f_entries_applied": applied,
            "first_run_gate": status, "first_run_behaviour": score, "first_run_persistent": failed, "error_classes": [],
            "routing": {}, "final_status": status, "total_cost": {"calls": 0, "tokens": 0, "usd": 0.0, "cost_quality": "unknown"},
            "rows_tried": [], "committed_record_id": rec_id if status == "committed" else "", "admitted_to_bank": False,
            "notes": ["backfilled from task_execution.json (no search ran)"], "source": "backfill",
        })
    return cands, miles


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dirs", nargs="+", type=Path)
    ap.add_argument("--ledger", type=Path, default=None)
    ap.add_argument("--version", default=None, help="ledger version directory (default: the live table version)")
    args = ap.parse_args()
    version = args.version or table_version()
    vdir = (args.ledger or ledger_root_default()) / version
    existing = {(str(r.get("task_id")), str(r.get("milestone_id")), str(r.get("final_status")))
                for r in read_jsonl(vdir / "milestones.jsonl")}
    for run in args.run_dirs:
        cands, miles = records_for_run(run, existing=existing, version=version)
        if miles:
            append_jsonl(vdir / "candidates.jsonl", cands)
            append_jsonl(vdir / "milestones.jsonl", miles)
            existing.update((m["task_id"], m["milestone_id"], m["final_status"]) for m in miles)
        print(f"{run}: {len(miles)} milestone(s) backfilled: {[m['milestone_id'] for m in miles]}")


if __name__ == "__main__":
    main()
