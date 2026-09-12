"""Feature-first planning: the policy is a prompt variant, a split reason and a handoff text."""

from __future__ import annotations

import json

from orchestra.decomposition.realbench_plan import _milestone_brief  # type: ignore[attr-defined]
from orchestra.realbench.milestone_planner import (
    PlanningBrief,
    parse_plan_payload,
    planner_system_message,
    render_planner_prompt,
    split_policy_from_env,
)


def _brief() -> PlanningBrief:
    return PlanningBrief(task_id="demo", documents=[("PRD.md", "a web app: db, backend, frontend")], modules=["app.db"])


def test_feature_prompt_asks_for_two_or_more_modules_foundations_first() -> None:
    p = render_planner_prompt(brief=_brief(), agent_backend="codex_sdk", max_milestones=5, split_policy="feature")
    assert "feature-first" in p and "2 to\n5 milestones" in p.replace("**", "")
    assert "foundations first" in p and "feature_module" in p
    assert "One milestone remains the expected answer" not in p
    assert "modules" in planner_system_message("feature") and "prefer one" not in planner_system_message("feature")


def test_risk_prompt_is_unchanged_by_default() -> None:
    p = render_planner_prompt(brief=_brief(), agent_backend="codex_sdk", max_milestones=4)
    assert "The two reasons a milestone may exist" in p
    assert "One milestone remains the expected answer" in p
    assert "prefer one milestone" in planner_system_message()
    assert split_policy_from_env("risk") == "risk" and split_policy_from_env("nonsense") == "risk"


def _milestone(i: int, reason: str = "feature_module", rationale: str = "delivers X, verified by its own suite") -> dict:
    return {
        "milestone_id": f"m{i}",
        "title": f"M{i}",
        "objective": f"build part {i}",
        "split_reason": reason,
        "risk_rationale": rationale,
        "gate_level": "implementation",
        "template_id": "test_first",
        "acceptance": {"criteria": ["works"]},
        "agents": [
            {"slot": "test_author", "role": "test_author", "mandate": "suite"},
            {"slot": "builder", "role": "implementer", "mandate": "build"},
        ],
    }


def test_parser_keeps_a_four_milestone_feature_plan_and_chains_it() -> None:
    payload = {"rationale": "four features", "milestones": [_milestone(i) for i in range(1, 5)]}
    draft = parse_plan_payload(json.dumps(payload), max_milestones=5)
    assert [m.milestone_id for m in draft.milestones] == ["m1", "m2", "m3", "m4"]
    assert all(m.split_reason == "feature_module" for m in draft.milestones)
    assert draft.milestones[-1].gate_level == "integration"
    assert draft.milestones[2].depends_on == ["m2"]


def test_parser_still_collapses_an_unargued_split() -> None:
    payload = {"milestones": [_milestone(1, rationale=""), _milestone(2, rationale="")]}
    draft = parse_plan_payload(json.dumps(payload), max_milestones=5)
    assert [m.milestone_id for m in draft.milestones] == ["m2"]


def test_feature_milestone_handoff_says_what_it_delivers() -> None:
    payload = {"milestones": [_milestone(1), _milestone(2)]}
    draft = parse_plan_payload(json.dumps(payload), max_milestones=5)
    text = _milestone_brief(draft.milestones[0], [])
    assert "What this milestone delivers" in text and "delivers X" in text
    assert "extend it, not redesign it" in text
