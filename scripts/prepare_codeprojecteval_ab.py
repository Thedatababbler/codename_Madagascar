#!/usr/bin/env python3
"""Freeze the three arms of the decomposition experiment, one set per repository.

Samples the risk-first planner until it produces a genuine multi-milestone plan,
then derives both controls from it: a single-segment arm holding the same agents
behind one final gate, and a single-agent arm handing the whole repository to one
implementer. Every arm comes from the same draft, so none of them carries planner
sampling variance.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from orchestra.codeprojecteval.ab import (
    merge_to_single_agent,
    merge_to_single_milestone,
    save_draft,
    total_budget,
)
from orchestra.codeprojecteval.ceiling import analyze_ceiling
from orchestra.codeprojecteval.dataset import DEFAULT_DATASET_ROOT, load_task
from orchestra.codeprojecteval.planning import cpe_brief
from orchestra.realbench.milestone_planner import plan_milestones


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", required=True, help="Comma-separated repository names.")
    ap.add_argument("--dataset-root", type=Path, default=DEFAULT_DATASET_ROOT)
    ap.add_argument("--backend", default="codex_sdk")
    ap.add_argument("--max-milestones", type=int, default=3)
    ap.add_argument("--attempts", type=int, default=4)
    ap.add_argument("--out", type=Path, default=Path("outputs/cpe_ab/plans"))
    args = ap.parse_args()

    report: dict[str, dict] = {}
    for name in [n.strip() for n in args.tasks.split(",") if n.strip()]:
        task = load_task(name, dataset_root=args.dataset_root)
        ceiling = analyze_ceiling(task)
        multi = None
        for attempt in range(1, args.attempts + 1):
            draft = plan_milestones(
                task_id=name,
                workspace=task.repo_root,
                agent_backend=args.backend,
                enable=True,
                max_milestones=args.max_milestones,
                brief=cpe_brief(task),
            )
            if draft is None:
                print(f"{name}: planner unavailable (attempt {attempt})")
                continue
            if len(draft.milestones) > 1:
                multi = draft
                break
            print(f"{name}: attempt {attempt} returned a single milestone; resampling")
        if multi is None:
            report[name] = {"status": "no_multi_plan", "ceiling": ceiling.reachable_ceiling}
            print(f"{name}: no multi-milestone plan after {args.attempts} attempts")
            continue

        single = merge_to_single_milestone(multi)
        solo = merge_to_single_agent(multi)
        multi_path = save_draft(multi, args.out / f"{name}.multi.json")
        single_path = save_draft(single, args.out / f"{name}.single.json")
        solo_path = save_draft(solo, args.out / f"{name}.solo.json")
        budgets = {
            "multi": total_budget(multi),
            "single": total_budget(single),
            "solo": total_budget(solo),
        }
        matched = (
            budgets["multi"]["agent_turns"] == budgets["single"]["agent_turns"]
            and budgets["multi"]["max_tokens"] == budgets["single"]["max_tokens"]
            # solo trades agents for one agent holding the same wall clock, the
            # only budget the Codex backend actually enforces.
            and budgets["solo"]["agent_turns"] == 1
            and budgets["solo"]["timeout_seconds"] == budgets["multi"]["timeout_seconds"]
        )
        report[name] = {
            "status": "ok",
            "ceiling": ceiling.reachable_ceiling,
            "milestones_multi": [m.milestone_id for m in multi.milestones],
            "risk_rationales": [m.risk_rationale[:200] for m in multi.milestones],
            "budgets": budgets,
            "budget_matched": matched,
            "multi_plan": str(multi_path),
            "single_plan": str(single_path),
            "solo_plan": str(solo_path),
        }
        print(
            f"{name}: multi={len(multi.milestones)} milestones, "
            f"agent_turns={budgets['multi']['agent_turns']}, "
            f"tokens={budgets['multi']['max_tokens']}, "
            f"budget_matched={matched}, ceiling={ceiling.reachable_ceiling:.2f}"
        )

    out = args.out / "ab_plans.json"
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
