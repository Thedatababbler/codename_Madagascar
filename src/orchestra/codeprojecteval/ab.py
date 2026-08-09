"""Build the two arms of a decomposition A/B from one planner draft.

The question is whether *gating* helps, not whether more compute helps. So the
single-segment arm is derived from the multi-segment plan by merging its
milestones: both arms run the same agents, in the same order, with the same
token and step budgets. They differ only in whether an acceptance gate and a
commit sit between those agents.

Deriving one arm from the other also removes planner sampling variance from the
comparison — the planner is asked once per repository, not once per run.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from orchestra.realbench.milestone_planner import (
    MAX_AGENTS_PER_MILESTONE,
    MilestoneAcceptance,
    MilestoneDraft,
    MilestonePlanDraft,
    parse_plan_payload,
)


def merge_to_single_milestone(
    draft: MilestonePlanDraft, *, milestone_id: str = "implement_repository"
) -> MilestonePlanDraft:
    """Collapse a multi-milestone plan into one budget-identical milestone."""
    milestones = list(draft.milestones)
    if not milestones:
        raise ValueError("cannot merge an empty plan")
    if len(milestones) == 1:
        return replace(
            draft,
            milestones=[replace(milestones[0], role="integration", depends_on=[])],
            generator=f"{draft.generator}+single_arm",
        )

    objective = "\n".join(
        f"Stage {index + 1} — {m.title}: {m.objective}"
        for index, m in enumerate(milestones)
    )
    criteria: list[str] = []
    corner_cases: list[str] = []
    checks: list[dict] = []
    focus: list[str] = []
    agents = []
    for milestone in milestones:
        for item in milestone.acceptance.criteria:
            if item not in criteria:
                criteria.append(item)
        for item in milestone.acceptance.corner_cases:
            if item not in corner_cases:
                corner_cases.append(item)
        for check in milestone.acceptance.checks:
            if check not in checks:
                checks.append(check)
        for path in milestone.focus_paths:
            if path not in focus:
                focus.append(path)
        agents.extend(milestone.agents)

    # Role ids must stay unique inside one subgraph; two milestones may both
    # have named their agent `implementer`.
    seen: dict[str, int] = {}
    unique_agents = []
    for agent in agents:
        count = seen.get(agent.role_id, 0) + 1
        seen[agent.role_id] = count
        unique_agents.append(
            agent if count == 1 else replace(agent, role_id=f"{agent.role_id}_{count}")
        )

    merged = MilestoneDraft(
        milestone_id=milestone_id,
        title="Implement the repository",
        objective=objective,
        risk_rationale="",
        role="integration",
        depends_on=[],
        focus_paths=focus,
        acceptance=MilestoneAcceptance(
            criteria=criteria, corner_cases=corner_cases, checks=checks
        ),
        agents=unique_agents,
    )
    return MilestonePlanDraft(
        milestones=[merged],
        rationale=(
            "Single-segment control arm: same agents, order and budgets as the "
            f"multi-segment plan, without intermediate gates. {draft.rationale}"
        ),
        generator=f"{draft.generator}+single_arm",
    )


def save_draft(draft: MilestonePlanDraft, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(draft.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return path


def load_draft(path: Path, *, max_milestones: int = 6) -> MilestonePlanDraft:
    """Re-validate a frozen draft through the same normalisation as a fresh one.

    The per-milestone agent cap is raised to the frozen plan's own widest
    milestone: the merged single-segment arm concentrates every agent into one
    milestone by construction, and clamping it to the planner's limit would
    hand the control arm less compute than the arm it is compared against.
    """
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    widest = max(
        (len(m.get("agents") or []) for m in payload.get("milestones") or []),
        default=MAX_AGENTS_PER_MILESTONE,
    )
    draft = parse_plan_payload(
        json.dumps(payload),
        max_milestones=max_milestones,
        max_agents=max(widest, MAX_AGENTS_PER_MILESTONE),
    )
    return replace(draft, generator=str(payload.get("generator") or draft.generator))


def total_budget(draft: MilestonePlanDraft) -> dict[str, float]:
    """Agent turns and token budget across the whole plan, for arm comparison."""
    agents = [agent for m in draft.milestones for agent in m.agents]
    return {
        "agent_turns": len(agents),
        "max_tokens": sum(a.max_tokens for a in agents),
        "max_steps": sum(a.max_steps for a in agents),
        "timeout_seconds": sum(a.timeout_seconds for a in agents),
    }
