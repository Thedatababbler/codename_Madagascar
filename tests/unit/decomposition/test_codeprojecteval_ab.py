"""The A/B arms must differ in gating only, never in budget."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from orchestra.codeprojecteval.ab import (
    load_draft,
    merge_to_single_milestone,
    save_draft,
    total_budget,
)
from orchestra.realbench.milestone_planner import (
    MAX_AGENTS_PER_MILESTONE,
    AgentDraft,
    MilestoneAcceptance,
    MilestoneDraft,
    MilestonePlanDraft,
)
from orchestra.roles.templates import default_templates


def _multi_plan() -> MilestonePlanDraft:
    first = MilestoneDraft(
        milestone_id="storage_contract",
        title="Storage contract",
        objective="Freeze the on-disk page format.",
        risk_rationale="Every later module builds on this layout.",
        gate_level="implementation",
        focus_paths=["pkg/storage.py"],
        acceptance=MilestoneAcceptance(
            criteria=["pages round-trip"],
            corner_cases=["empty page"],
            checks=[{"type": "import", "module": "pkg.storage"}],
        ),
        agents=[
            AgentDraft(
                role_id="implementer",
                title="Storage implementer",
                mandate="Write the page codec.",
                max_tokens=8192,
                max_steps=12,
                timeout_seconds=1200.0,
            )
        ],
    )
    second = MilestoneDraft(
        milestone_id="tree_api",
        title="Tree API",
        objective="Implement the public tree operations.",
        risk_rationale="Consumers depend on the documented surface.",
        gate_level="integration",
        depends_on=["storage_contract"],
        focus_paths=["pkg/tree.py"],
        acceptance=MilestoneAcceptance(
            criteria=["insert then get returns the value"],
            checks=[{"type": "import", "module": "pkg.tree"}],
        ),
        agents=[
            AgentDraft(
                role_id="implementer",
                title="Tree implementer",
                mandate="Write the tree operations.",
                max_tokens=8192,
                max_steps=12,
                timeout_seconds=1200.0,
            )
        ],
    )
    return MilestonePlanDraft(milestones=[first, second], rationale="two risk gates")


def test_single_arm_keeps_every_agent_and_the_whole_budget() -> None:
    multi = _multi_plan()
    single = merge_to_single_milestone(multi)

    assert len(single.milestones) == 1
    assert total_budget(single) == total_budget(multi)
    assert total_budget(single)["agent_turns"] == 2


def test_single_arm_carries_the_full_objective_and_acceptance() -> None:
    single = merge_to_single_milestone(_multi_plan()).milestones[0]

    assert "page format" in single.objective and "tree operations" in single.objective
    assert single.acceptance.criteria == [
        "pages round-trip",
        "insert then get returns the value",
    ]
    assert single.acceptance.corner_cases == ["empty page"]
    assert len(single.acceptance.checks) == 2
    assert single.focus_paths == ["pkg/storage.py", "pkg/tree.py"]
    # A terminal milestone is graded at integration level.
    assert single.role == "integration"
    assert single.depends_on == []


def test_duplicate_agent_role_ids_are_disambiguated() -> None:
    """Both milestones named their agent `implementer`; one subgraph needs unique ids."""
    single = merge_to_single_milestone(_multi_plan()).milestones[0]

    role_ids = [agent.role_id for agent in single.agents]
    assert role_ids == ["implementer", "implementer_2"]


def test_frozen_plans_round_trip(tmp_path: Path) -> None:
    multi = _multi_plan()
    path = save_draft(multi, tmp_path / "plan.multi.json")
    restored = load_draft(path)

    assert [m.milestone_id for m in restored.milestones] == [
        "storage_contract",
        "tree_api",
    ]
    assert total_budget(restored) == total_budget(multi)


def test_merged_arm_keeps_its_budget_through_a_reload(tmp_path: Path) -> None:
    """The control arm must not lose agents to the planner's per-milestone cap.

    Merging concentrates every agent into one milestone, so a plan wide enough
    to exceed that cap would come back from disk with fewer turns than the arm
    it is compared against -- an unequal-compute comparison that looks like a
    result.
    """
    base = _multi_plan()
    wide = replace(
        base,
        milestones=[
            replace(
                milestone,
                agents=[
                    replace(agent, role_id=f"{agent.role_id}{index}")
                    for index, agent in enumerate(milestone.agents * 3)
                ],
            )
            for milestone in base.milestones
        ],
    )
    single = merge_to_single_milestone(wide)
    assert len(single.milestones[0].agents) > MAX_AGENTS_PER_MILESTONE

    restored = load_draft(save_draft(single, tmp_path / "plan.single.json"))

    assert total_budget(restored) == total_budget(wide)


def test_merging_rebinds_agents_onto_slots_the_chain_actually_has(
    tmp_path: Path,
) -> None:
    """Every merged agent must land on a real slot of an extensible template.

    A merged plan that kept a one-slot template would drop its overflow on the
    next load, and a slot id spelled out by hand would unbind the moment
    chain.yaml renamed one.
    """
    single = merge_to_single_milestone(_multi_plan())
    milestone = single.milestones[0]
    template = default_templates()[milestone.template_id]
    valid = {slot.slot_id for slot in template.slots_for(len(milestone.agents))}

    assert template.extensible
    assert {agent.slot_id for agent in milestone.agents} <= valid

    restored = load_draft(save_draft(single, tmp_path / "plan.single.json"))
    assert [a.role for a in restored.milestones[0].agents] == [
        a.role for a in milestone.agents
    ]
