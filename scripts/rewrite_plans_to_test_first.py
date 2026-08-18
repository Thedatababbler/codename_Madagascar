#!/usr/bin/env python3
"""Put an authored test suite in front of the builder, in the frozen plans.

The quality trigger and the Pareto quality axis both read the behavioural stage of a
milestone's graded score. Without an authored suite that stage is the dataset's
visible `check_tests`, which the recorded runs pass 9/9, so behaviour reads 1.0 and
there is nothing to search or rank (EXP-20260810-05). Only the `test_first` template
authors a suite, and no frozen plan selects it: the template postdates them, and the
planner is instructed to prefer the cheapest template that addresses the risk.

Replanning would cost API calls and might still decline, so the plans are rewritten
instead. This is a deliberate experimental control, not a claim about what the planner
would choose: the milestone decomposition, acceptance checks and risk rationales are
left exactly as the planner wrote them, and only the subgraph shape changes.

Roles carry over rather than being reassigned. Every builder in the four two-milestone
plans is already in `test_first`'s builder slot (`contract_author`, `implementer`,
`test_driven_implementer`), and every repairer in its repairer slot, so nothing has to
be invented. A `review_then_fix` milestone loses its auditor slot, which is the real
cost of the swap and is reported per milestone.

    uv run python scripts/rewrite_plans_to_test_first.py --check    # report only
    uv run python scripts/rewrite_plans_to_test_first.py --write
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from orchestra.codeprojecteval.ab import load_draft
from orchestra.roles.templates import default_templates

TEMPLATE_ID = "test_first"
DEFAULT_PLANS = [
    Path("outputs/cpe_tuning/plans/imapclient.multi.json"),
    Path("outputs/cpe_ab/plans/pyjwt.multi.json"),
    Path("outputs/cpe_ab/plans/simpy.multi.json"),
    Path("outputs/cpe_ab/plans/bplustree.multi.json"),
]


def _slot_roles() -> dict[str, set[str]]:
    template = default_templates()[TEMPLATE_ID]
    return {slot.slot_id: set(slot.allowed_roles) for slot in template.slots}


def _author_mandate(milestone: dict[str, Any]) -> str:
    """What the test author is told, built from what the planner already wrote.

    Deliberately derived from the milestone's own acceptance criteria rather than
    written fresh: the suite is meant to be this milestone's yardstick, and a mandate
    invented here would be a second, competing statement of what done means.
    """
    criteria = (milestone.get("acceptance") or {}).get("criteria") or []
    focus = milestone.get("focus_paths") or []
    parts = [
        "Write the executable suite that decides whether this milestone succeeded, "
        "from the design documents alone, before any implementation exists.",
    ]
    if milestone.get("risk_rationale"):
        parts.append(f"The risk this milestone gates: {milestone['risk_rationale']}")
    if criteria:
        parts.append(
            "Cover every acceptance criterion:\n"
            + "\n".join(f"- {c}" for c in criteria)
        )
    if focus:
        parts.append("Modules in scope:\n" + "\n".join(f"- {p}" for p in focus))
    return "\n\n".join(parts)


# `test_driven_implementer` fitted this slot while the authored suite stayed in
# the workspace. It no longer does: custody moves the suite out before the
# builder starts, and that role's premise is a suite it can read. The agent is
# rebound rather than dropped — it is still the milestone's implementer, and
# dropping it would silently hand this arm less compute than the plan says.
_REBIND = {"test_driven_implementer": "implementer"}


def _pick(agents: list[dict[str, Any]], allowed: set[str]) -> dict[str, Any] | None:
    for agent in agents:
        role = str(agent.get("role") or "")
        rebound = _REBIND.get(role, role)
        if rebound in allowed:
            if rebound != role:
                agent["role"] = rebound
                agent.pop("role_id", None)
                agent.pop("title", None)
            return agent
    return None


def rewrite_milestone(milestone: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """The milestone under `test_first`, plus the roles that had to be dropped."""
    slots = _slot_roles()
    agents = [a for a in (milestone.get("agents") or []) if isinstance(a, dict)]
    builder = _pick(agents, slots["builder"])
    repairer = _pick(
        [a for a in agents if a is not builder],
        slots["repairer"],
    )
    if builder is None:
        raise SystemExit(
            f"{milestone.get('milestone_id')}: no agent whose role fits the builder slot "
            f"(have {[a.get('role') for a in agents]}, need one of {sorted(slots['builder'])})"
        )

    kept = {id(builder), id(repairer)}
    dropped = [
        str(a.get("role") or "?") for a in agents if id(a) not in kept
    ]

    rewritten = [
        {
            "slot": "test_author",
            "role": "test_author",
            "mandate": _author_mandate(milestone),
            "focus_paths": list(milestone.get("focus_paths") or []),
        },
        {**builder, "slot": "builder"},
    ]
    if repairer is not None:
        rewritten.append({**repairer, "slot": "repairer"})

    out = dict(milestone)
    out["template_id"] = TEMPLATE_ID
    out["agents"] = rewritten
    return out, dropped


def rewrite_plan(path: Path) -> tuple[dict[str, Any], list[str]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    notes: list[str] = []
    milestones = []
    for milestone in payload.get("milestones") or []:
        rewritten, dropped = rewrite_milestone(milestone)
        milestones.append(rewritten)
        roles = [a.get("role") for a in rewritten["agents"]]
        note = (
            f"  {milestone.get('milestone_id')}: "
            f"{milestone.get('template_id')} -> {TEMPLATE_ID}, {roles}"
        )
        if dropped:
            note += f"   dropped: {dropped}"
        notes.append(note)
    payload["milestones"] = milestones
    return payload, notes


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="write the rewritten plans")
    ap.add_argument("plans", nargs="*", type=Path, default=None)
    args = ap.parse_args()
    plans = args.plans or DEFAULT_PLANS

    failures = 0
    for path in plans:
        if not path.is_file():
            print(f"{path}: missing")
            failures += 1
            continue
        payload, notes = rewrite_plan(path)
        print(f"{path}")
        for note in notes:
            print(note)

        target = path.with_suffix(".test_first.json")
        target.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        # Re-validated through the same path a run takes, so a plan that would be
        # silently normalised into something else is caught here rather than after
        # an hour of agent time.
        draft = load_draft(target)
        for milestone in draft.milestones:
            slots = [a.slot_id for a in milestone.agents]
            roles = [a.role for a in milestone.agents]
            if milestone.template_id != TEMPLATE_ID or "test_author" not in roles:
                print(f"  REJECTED after reload: template={milestone.template_id} roles={roles}")
                failures += 1
            else:
                print(f"  reloaded ok: slots={slots} roles={roles}")
        if not args.write:
            target.unlink()
        else:
            print(f"  wrote {target}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
