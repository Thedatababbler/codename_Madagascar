"""The suite-quality probe replays one milestone with the test author alone."""

from __future__ import annotations

import json

from orchestra.realbench.milestone_planner import parse_plan_payload
from orchestra.realbench.subgraph_builder import _system_prompt, default_templates


def _ms(mid: str, gate: str, split: str = "feature_module", depends_on=()):
    return {
        "milestone_id": mid, "title": mid, "objective": "all", "gate_level": gate,
        "split_reason": split, "risk_rationale": "delivers the module",
        "template_id": "author_only", "acceptance": {"criteria": ["works"]},
        "depends_on": list(depends_on),
        "agents": [{"slot": "test_author", "role": "test_author", "mandate": "suite"}],
    }


def _draft(gate: str, split: str = "feature_module"):
    # The parser makes the last milestone the integration one, so an
    # implementation probe is followed by a trailing milestone exactly as
    # scripts/author_probe.py lays it out.
    milestones = [_ms("only", gate, split)]
    if gate != "integration":
        milestones.append(_ms("tail", "integration", split, depends_on=["only"]))
    return parse_plan_payload(json.dumps({"milestones": milestones}), max_milestones=5)


def test_author_only_is_not_a_planner_choice_but_replays() -> None:
    templates = default_templates()
    assert "author_only" in templates and not templates["author_only"].planner_selectable
    draft = _draft("implementation")
    milestone = draft.milestones[0]
    assert milestone.template_id == "author_only"
    assert [a.role for a in milestone.agents] == ["test_author"]


def test_probe_integration_milestone_gets_the_breadth_brief_without_dependencies() -> None:
    milestone = _draft("integration").milestones[0]
    assert not milestone.depends_on
    prompt = _system_prompt(milestone=milestone, agent=milestone.agents[0], agent_backend="codex_sdk")
    assert "This is the integration milestone" in prompt
    assert "no priming call, no shim, no reaching inside" in prompt


def test_every_milestone_author_gets_the_no_workaround_rules() -> None:
    milestone = _draft("implementation").milestones[0]
    prompt = _system_prompt(milestone=milestone, agent=milestone.agents[0], agent_backend="codex_sdk")
    assert "Use the library the way its user would" in prompt
    assert "register_builtins()" in prompt and "sys.path.insert" in prompt
    assert "This is the integration milestone" not in prompt
