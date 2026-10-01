#!/usr/bin/env python3
"""Run one design cycle (self-evolution spec §9) or roll back a published version.

Report-only is the default: the proposed tables, the ranking and the change
summary are written under ``outputs/evolution/cycles/<cycle_id>/`` and
nothing under ``configs/playbook_v2`` changes. ``--publish`` writes the next
immutable version and points the live tables at it. Re-runs and the evolver
cost model calls and are off unless asked for.

    uv run python scripts/run_design_cycle.py --config configs/experiments/codeprojecteval_milestones_evolution.yaml
    uv run python scripts/run_design_cycle.py --config ... --launch-reruns --evolver --publish
    uv run python scripts/run_design_cycle.py --config ... --rollback v2 --reason "batch net fix fell"
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml

from orchestra.control.evolution.design_cycle import (
    CycleConfig,
    current_version,
    load_milestone_records,
    rollback,
    run_cycle,
    should_trigger,
)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True, type=Path)
    ap.add_argument("--cycle-id", default=None)
    ap.add_argument("--publish", action="store_true", help="write the next version (default: report only)")
    ap.add_argument("--launch-reruns", action="store_true", help="actually launch the planned bank re-runs (paid)")
    ap.add_argument("--evolver", action="store_true", help="ask the evolver agent for proposals (paid)")
    ap.add_argument("--force", action="store_true", help="run even when the §9.1 trigger is not met")
    ap.add_argument("--seen-tasks", type=Path, default=None, help="default: <evolution root>/cycles/seen_tasks.json")
    ap.add_argument("--new-failures", type=int, default=0, help="failed milestones added to the bank since the last cycle")
    ap.add_argument("--rollback", default=None, metavar="vN")
    ap.add_argument("--reason", default="")
    args = ap.parse_args()
    config = yaml.safe_load(args.config.read_text(encoding="utf-8")) or {}
    cfg = CycleConfig.from_config(config)

    if args.rollback:
        version, changes = rollback(args.rollback, reason=args.reason)
        print(f"rolled back {changes['rollback_of']} -> {changes['restored']} as {version}; demoted rows {changes['demoted_rows']}")
        return

    from orchestra.control.evolution.ledger import evolution_root

    args.seen_tasks = args.seen_tasks or evolution_root() / "cycles" / "seen_tasks.json"
    seen = json.loads(args.seen_tasks.read_text(encoding="utf-8")) if args.seen_tasks.is_file() else []
    miles = load_milestone_records()
    ok, why = should_trigger(miles, seen_tasks=seen, new_failures=args.new_failures, cfg=cfg)
    print(f"trigger: {ok} ({why}); current version {current_version()}")
    if not ok and not args.force:
        print("not triggered; pass --force to run anyway")
        return
    result = run_cycle(cfg, cycle_id=args.cycle_id, publish=args.publish, launch_reruns=args.launch_reruns,
                       run_evolver=args.evolver, rerun_config=str(args.config))
    tasks = sorted({str(r.get("task_id")) for r in miles} | set(seen))
    args.seen_tasks.parent.mkdir(parents=True, exist_ok=True)
    args.seen_tasks.write_text(json.dumps(tasks), encoding="utf-8")
    print(f"cycle {result.cycle_id}: {'published ' + str(result.version) if result.published else 'report only'}")
    print(f"row changes: {[(c.row_id, c.before, c.after) for c in result.row_changes]}")
    print(f"entry changes: {[(c['entry_id'], c['before'], c['after']) for c in result.entry_changes]}")
    print(f"re-runs planned: {len(result.rerun_jobs)}; tripwire: {result.tripwire}")
    print(f"report: {result.report_path}")


if __name__ == "__main__":
    main()
