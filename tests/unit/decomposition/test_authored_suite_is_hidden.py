"""Who hears about `spec_tests/`, and who must not.

Two runs shaped these tests. In the first (EXP-20260810-06) every builder
deleted the authored suite before the gate could copy it out: the shipping rule
they follow forbids files no design document describes, and no document
describes `spec_tests/`, so a tidy agent removes it. The author therefore gets
told the directory is expected.

In the second (EXP-20260811-01) the suite survived and turned out to be
worthless as a yardstick — the implementers passed 38/38, 29/29 and 51/51 of it
while failing most of the held-out suite, because a test you can read and run is
a test you satisfy. So it is now moved out of the workspace between the author
and the implementer, and every later agent hears nothing about it: there is
nothing left for them to preserve, and naming a directory they cannot see would
only send them looking for it.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import yaml

from orchestra.realbench.milestone_planner import parse_plan_payload
from orchestra.realbench.subgraph_builder import (
    CODEPROJECTEVAL_PROMPT_PROFILE,
    REALBENCH_PROMPT_PROFILE,
    DatasetPromptProfile,
    materialize_milestone_subgraph,
    prepare_generated_root,
)

TEST_FIRST_AGENTS = [
    {"slot": "test_author", "role": "test_author", "mandate": "write the suite"},
    {"slot": "builder", "role": "contract_author", "mandate": "implement it"},
    {"slot": "repairer", "role": "gate_repairer", "mandate": "fix the gate"},
]


def _prompts(
    template_id: str,
    agents: list[dict],
    *,
    profile: DatasetPromptProfile = CODEPROJECTEVAL_PROMPT_PROFILE,
) -> dict[str, str]:
    """Rendered system prompt per role, as the run writes it to disk."""
    plan = parse_plan_payload(
        {
            "milestones": [
                {
                    "milestone_id": "m",
                    "objective": "build it",
                    "risk_rationale": "r",
                    "template_id": template_id,
                    "agents": agents,
                }
            ]
        },
        max_agents=4,
    )
    root = prepare_generated_root(
        Path(tempfile.mkdtemp()), base_contracts_dir="configs/contracts"
    )
    _path, roster = materialize_milestone_subgraph(
        generated_root=root,
        milestone=plan.milestones[0],
        agent_backend="codex_sdk",
        harness_command=["python", "check.py", "--spec-tests", "/tmp/frozen"],
        profile=profile,
    )
    prompts = {}
    for entry in roster:
        contract = yaml.safe_load(
            Path(entry["contract_path"]).read_text(encoding="utf-8")
        )
        prompts[entry["role_id"]] = contract["system_prompt_template"]
    return prompts


def test_the_author_is_told_its_new_directory_is_expected() -> None:
    """It writes the one directory the shipping rule would otherwise forbid."""
    prompt = _prompts("test_first", TEST_FIRST_AGENTS)["test_author_test_author"]

    assert "`spec_tests/`" in prompt
    assert "must be present in the repository when you stop" in prompt


def test_the_exemption_comes_after_the_rule_it_exempts() -> None:
    """Order carries the meaning: an exception stated first reads as overridden."""
    prompt = _prompts("test_first", TEST_FIRST_AGENTS)["test_author_test_author"]

    rule = prompt.index("Ship only files docs/directory_tree.txt describes")
    exemption = prompt.index("`spec_tests/` is the exception")
    assert rule < exemption


def test_the_author_is_told_the_suite_leaves_the_workspace() -> None:
    """Otherwise "it must be there when you stop" reads as "it ships"."""
    prompt = _prompts("test_first", TEST_FIRST_AGENTS)["test_author_test_author"]

    assert "moved out of the workspace" in prompt
    assert "no later agent sees it" in prompt


def test_the_builder_hears_nothing_about_the_suite() -> None:
    """It is ranked on that suite, so it must not be able to aim at it."""
    prompt = _prompts("test_first", TEST_FIRST_AGENTS)["builder_contract_author"]

    assert "spec_tests" not in prompt


def test_the_repairer_hears_nothing_about_the_suite_either() -> None:
    prompt = _prompts("test_first", TEST_FIRST_AGENTS)["repairer_gate_repairer"]

    assert "spec_tests" not in prompt


def test_a_milestone_with_no_test_author_hears_nothing_about_a_suite() -> None:
    """Naming a directory that will not exist would send an agent looking."""
    prompts = _prompts(
        "gate_then_repair",
        [
            {"slot": "author", "role": "implementer", "mandate": "a"},
            {"slot": "repairer", "role": "gate_repairer", "mandate": "b"},
        ],
    )

    assert prompts
    for prompt in prompts.values():
        assert "spec_tests" not in prompt


def test_realbench_author_is_told_the_directory_is_expected() -> None:
    """Same exemption, different shipping rule: the public tree, not directory_tree."""
    prompt = _prompts(
        "test_first", TEST_FIRST_AGENTS, profile=REALBENCH_PROMPT_PROFILE
    )["test_author_test_author"]

    rule = prompt.index("Ship only files the public tree describes")
    exemption = prompt.index("`spec_tests/` is the exception")
    assert rule < exemption
    assert "must be present in the repository when you stop" in prompt


def test_realbench_builder_hears_nothing_about_the_suite() -> None:
    prompt = _prompts(
        "test_first", TEST_FIRST_AGENTS, profile=REALBENCH_PROMPT_PROFILE
    )["builder_contract_author"]

    assert "spec_tests" not in prompt
    assert "structural" in prompt
