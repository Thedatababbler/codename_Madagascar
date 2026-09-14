"""The integration milestone's test author is told to cover the finished library's breadth."""

from __future__ import annotations

import json

from orchestra.realbench.milestone_planner import parse_plan_payload
from orchestra.realbench.subgraph_builder import _system_prompt


def _plan():
    def ms(i, gate):
        return {
            "milestone_id": f"m{i}", "title": f"M{i}", "objective": f"part {i}",
            "split_reason": "feature_module", "risk_rationale": "delivers part",
            "gate_level": gate, "template_id": "test_first", "acceptance": {"criteria": ["works"]},
            "agents": [
                {"slot": "test_author", "role": "test_author", "mandate": "suite"},
                {"slot": "builder", "role": "implementer", "mandate": "build"},
            ],
        }
    return parse_plan_payload(json.dumps({"milestones": [ms(1, "implementation"), ms(2, "integration")]}), max_milestones=5)


def _prompt(milestone, role):
    agent = next(a for a in milestone.agents if a.role == role)
    return _system_prompt(milestone=milestone, agent=agent, agent_backend="codex_sdk")


def test_integration_test_author_gets_the_breadth_brief() -> None:
    first, last = _plan().milestones
    assert last.gate_level == "integration" and last.depends_on
    assert "This is the integration milestone" in _prompt(last, "test_author")
    assert "object protocol" in _prompt(last, "test_author")


def test_no_breadth_brief_for_other_roles_or_earlier_milestones() -> None:
    first, last = _plan().milestones
    assert "This is the integration milestone" not in _prompt(last, "implementer")
    assert "This is the integration milestone" not in _prompt(first, "test_author")


def test_single_milestone_plan_is_unchanged() -> None:
    draft = parse_plan_payload(json.dumps({"milestones": [{
        "milestone_id": "only", "title": "Only", "objective": "all", "gate_level": "integration",
        "template_id": "test_first", "acceptance": {"criteria": ["works"]},
        "agents": [{"slot": "test_author", "role": "test_author", "mandate": "suite"},
                   {"slot": "builder", "role": "implementer", "mandate": "build"}],
    }]}), max_milestones=5)
    assert "This is the integration milestone" not in _prompt(draft.milestones[0], "test_author")
