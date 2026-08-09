"""Build the arms of a decomposition experiment from one planner draft.

Three arms, each answering a different question:

``solo``
    One agent, the whole repository, the plan's combined wall-clock budget. The
    plain-Codex reference point; if neither other arm beats this, the machinery
    is not paying for itself.
``single``
    Every agent of the multi-segment plan, in order, inside one milestone. Same
    collaborators as ``multi`` but only one gate, at the very end.
``multi``
    The planner's own segmentation, with a gate and a commit per milestone.

``single`` isolates *gating* from *more agents*, which ``solo`` alone cannot:
comparing solo against multi would confound the two. Deriving every arm from one
draft also removes planner sampling variance — the planner is asked once per
repository, not once per run.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from orchestra.realbench.milestone_planner import (
    MAX_AGENTS_PER_MILESTONE,
    MAX_STEPS_RANGE,
    MAX_TOKENS_RANGE,
    TIMEOUT_RANGE,
    MilestoneAcceptance,
    MilestoneDraft,
    MilestonePlanDraft,
    parse_plan_payload,
)
from orchestra.roles.templates import FALLBACK_TEMPLATE_ID, default_templates


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
            milestones=[replace(milestones[0], gate_level="integration", depends_on=[])],
            generator=f"{draft.generator}+single_arm",
        )

    # Slot the merged agents into an extensible chain. Keeping a template that
    # declares fewer slots -- `solo` above all -- would drop the overflow the
    # next time this plan is loaded, handing the control arm less compute than
    # the arm it exists to be compared against.
    template_id = FALLBACK_TEMPLATE_ID

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
    # Slot ids come from the template itself rather than being spelled out here,
    # so renaming a slot in chain.yaml cannot silently unbind these agents.
    chain_slots = default_templates()[template_id].slots_for(len(agents))
    unique_agents = []
    for index, agent in enumerate(agents):
        count = seen.get(agent.role_id, 0) + 1
        seen[agent.role_id] = count
        unique_agents.append(
            replace(
                agent,
                slot_id=chain_slots[index].slot_id,
                role_id=agent.role_id if count == 1 else f"{agent.role_id}_{count}",
            )
        )

    merged = MilestoneDraft(
        milestone_id=milestone_id,
        title="Implement the repository",
        objective=objective,
        risk_rationale="",
        gate_level="integration",
        template_id=template_id,
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


def merge_to_single_agent(
    draft: MilestonePlanDraft, *, milestone_id: str = "implement_repository"
) -> MilestonePlanDraft:
    """Collapse a plan into one agent doing the whole repository end to end.

    This is the reference point the other two arms have to beat: plain Codex,
    handed the whole task once, with no second agent and no intermediate gate.

    It inherits the summed wall-clock budget of the plan it replaces. Under the
    Codex backend that is the *only* budget that binds -- ``max_tokens`` is read
    by the openai-compatible path alone and ``max_steps`` by smolagents alone --
    so giving this arm one agent's timeout would starve it on the one axis that
    is real and make the baseline a strawman.
    """
    single = merge_to_single_milestone(draft, milestone_id=milestone_id)
    merged = single.milestones[0]
    agents = list(merged.agents)
    if not agents:
        raise ValueError("cannot merge a plan with no agents")

    solo = replace(
        agents[0],
        role_id="solo_implementer",
        title="Sole implementer",
        role="implementer",
        slot_id=default_templates()["solo"].slots[0].slot_id,
        mandate=(
            "Implement the entire repository yourself, end to end. No other "
            "agent will run after you and there is no intermediate checkpoint: "
            "everything the acceptance suite needs must be in place when you "
            "finish."
        ),
        focus_paths=list(merged.focus_paths),
        max_tokens=sum(a.max_tokens for a in agents),
        max_steps=sum(a.max_steps for a in agents),
        timeout_seconds=sum(a.timeout_seconds for a in agents),
    )
    return MilestonePlanDraft(
        milestones=[replace(merged, template_id="solo", agents=[solo])],
        rationale=(
            "Single-agent baseline: one agent implements the whole repository "
            "with the plan's combined wall-clock budget, no collaborators and "
            f"no intermediate gate. {draft.rationale}"
        ),
        generator=f"{draft.generator}+solo_arm",
    )


def save_draft(draft: MilestonePlanDraft, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(draft.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return path


def load_draft(path: Path, *, max_milestones: int = 6) -> MilestonePlanDraft:
    """Re-validate a frozen draft through the same normalisation as a fresh one.

    Both planner-facing caps are raised to whatever this plan already contains.
    The merged arms concentrate the whole plan into fewer nodes by construction
    -- every agent into one milestone, or the whole wall clock onto one agent --
    and re-clamping to the planner's per-proposal limits would hand a control
    arm less compute than the arm it exists to be compared against.
    """
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    milestones = payload.get("milestones") or []
    widest = max((len(m.get("agents") or []) for m in milestones), default=MAX_AGENTS_PER_MILESTONE)
    agents = [agent for m in milestones for agent in (m.get("agents") or [])]

    def _largest(field: str, default: float) -> float:
        return max((float(agent.get(field) or 0.0) for agent in agents), default=default)

    draft = parse_plan_payload(
        json.dumps(payload),
        max_milestones=max_milestones,
        max_agents=max(widest, MAX_AGENTS_PER_MILESTONE),
        timeout_ceiling=_largest("timeout_seconds", TIMEOUT_RANGE[1]),
        token_ceiling=int(_largest("max_tokens", MAX_TOKENS_RANGE[1])),
        step_ceiling=int(_largest("max_steps", MAX_STEPS_RANGE[1])),
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
