#!/usr/bin/env python3
"""Ask the risk-first planner how it would segment each CodeProjectEval repo.

Answers the prior question before spending a full generation batch: does this
dataset contain decisions with the blast radius a milestone is supposed to guard?
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from orchestra.codeprojecteval import cpe_brief, load_task
from orchestra.codeprojecteval.dataset import available_tasks
from orchestra.realbench.milestone_planner import plan_milestones
from orchestra.realbench.public_harness import parse_expected_modules


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", type=str, default="")
    ap.add_argument("--backend", type=str, default="codex_sdk")
    ap.add_argument("--out", type=Path, default=Path("outputs/cpe_planner_probe.json"))
    ap.add_argument("--split-policy", type=str, default="risk", choices=["risk", "feature"])
    ap.add_argument("--max-milestones", type=int, default=4)
    ap.add_argument("--plans-dir", type=Path, default=None,
                    help="save each draft as <task>.plan.json, usable with run_codeprojecteval_decomp --plan-file")
    ap.add_argument("--dataset-root", type=Path, default=None)
    args = ap.parse_args()
    if args.dataset_root is not None:
        import os
        os.environ["CPE_DATASET_ROOT"] = str(args.dataset_root)

    names = [n.strip() for n in args.tasks.split(",") if n.strip()] or available_tasks()
    results: dict[str, dict] = {}
    for name in names:
        task = load_task(name)
        brief = cpe_brief(task)
        modules = parse_expected_modules(task.directory_tree)
        loc = sum(
            len(p.read_text(encoding="utf-8", errors="replace").splitlines())
            for p in (task.repo_root / task.source_dir).rglob("*.py")
        ) if task.source_dir and (task.repo_root / task.source_dir).exists() else 0
        draft = plan_milestones(
            task_id=name,
            workspace=task.repo_root,
            agent_backend=args.backend,
            brief=brief,
            enable=True,
            max_milestones=args.max_milestones,
            split_policy=args.split_policy,
        )
        if draft is not None and args.plans_dir is not None:
            args.plans_dir.mkdir(parents=True, exist_ok=True)
            (args.plans_dir / f"{name}.plan.json").write_text(
                json.dumps({"generator": "llm", "split_policy": args.split_policy, **draft.to_dict()},
                           indent=2, ensure_ascii=False), encoding="utf-8")
        if draft is None:
            results[name] = {"modules": len(modules), "loc": loc, "draft": None}
            print(f"{name:32s} modules={len(modules):3d} loc={loc:5d} planner=UNAVAILABLE")
            continue
        results[name] = {
            "modules": len(modules),
            "loc": loc,
            "milestones": [
                {
                    "id": m.milestone_id,
                    "split_reason": m.split_reason,
                    "template": m.template_id,
                    "role": m.role,
                    "risk_rationale": m.risk_rationale,
                    "agents": len(m.agents),
                    "criteria": m.acceptance.criteria[:3],
                }
                for m in draft.milestones
            ],
            "rationale": draft.rationale,
        }
        gates = [m for m in draft.milestones if m.risk_rationale]
        print(
            f"{name:32s} modules={len(modules):3d} loc={loc:5d} "
            f"milestones={len(draft.milestones)} risk_gates={len(gates)}",
            flush=True,
        )
        for m in draft.milestones:
            if m.risk_rationale:
                print(f"    gate {m.milestone_id}: {m.risk_rationale[:150]}")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    split = [n for n, r in results.items() if len(r.get("milestones") or []) > 1]
    print(f"\nsplit {len(split)}/{len(results)}: {', '.join(split) or '(none)'}")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
