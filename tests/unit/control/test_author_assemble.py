"""Stage B0 of the author-evolution loop: the fixed rules split out of
``test_author.yaml`` and the assembled prompt is byte-identical to it when the
rules document is empty and no inventory is given."""

from __future__ import annotations

import tempfile
from pathlib import Path

import yaml

from orchestra.control.author.assemble import (
    INVENTORY_ENV,
    RULES_DOC_ENV,
    assemble_prompt,
    author_prompt,
    load_core_rules,
)
from orchestra.control.author.rules_doc import (
    Rule,
    RulesDoc,
    RulesDocError,
    current_version_dir,
    load_rules_doc,
    render_rules,
    write_rules_doc,
)
from orchestra.control.fast_loop.evolution_config import AuthorConfig, EvolutionConfig
from orchestra.realbench.milestone_planner import parse_plan_payload
from orchestra.realbench.subgraph_builder import _system_prompt
from orchestra.roles.pool import load_role_pool

YAML_PROMPT = yaml.safe_load(Path("configs/roles/test_author.yaml").read_text(encoding="utf-8"))["prompt"]


def test_core_rules_file_is_the_yaml_prompt() -> None:
    assert load_core_rules().strip() == YAML_PROMPT.strip()


def test_empty_rules_doc_and_no_inventory_assemble_to_the_yaml_prompt_byte_for_byte() -> None:
    pool_prompt = load_role_pool("configs/roles").require("test_author").prompt
    doc = load_rules_doc(current_version_dir())
    assert doc.rendered() == ()
    assert assemble_prompt(load_core_rules(), render_rules(doc.rendered()), "") == pool_prompt


def test_author_prompt_without_the_env_is_the_default_untouched() -> None:
    assert author_prompt("whatever", env={}) == "whatever"


def test_author_prompt_with_v0_equals_the_default() -> None:
    pool_prompt = load_role_pool("configs/roles").require("test_author").prompt
    assert author_prompt(pool_prompt, env={RULES_DOC_ENV: "configs/author/rules_doc/current"}) == pool_prompt


def _author_system_prompt() -> str:
    plan = parse_plan_payload(
        {"milestones": [{"milestone_id": "m", "objective": "build it", "risk_rationale": "r",
                         "template_id": "test_first",
                         "agents": [{"slot": "test_author", "role": "test_author", "mandate": "write"}]}]},
        max_agents=4,
    )
    m = plan.milestones[0]
    return _system_prompt(milestone=m, agent=m.agents[0], agent_backend="codex_sdk")


def test_system_prompt_identical_with_env_unset_and_with_v0(monkeypatch) -> None:
    monkeypatch.delenv(RULES_DOC_ENV, raising=False)
    monkeypatch.delenv(INVENTORY_ENV, raising=False)
    before = _author_system_prompt()
    monkeypatch.setenv(RULES_DOC_ENV, "configs/author/rules_doc/current")
    assert _author_system_prompt() == before


def test_rendered_rules_and_inventory_are_appended_in_order(monkeypatch) -> None:
    d = Path(tempfile.mkdtemp())
    write_rules_doc(RulesDoc("vX", (
        Rule("R-001", "active", "the sentence names a boundary", "test both sides of it", example="assert f(0) == 0"),
        Rule("R-002", "trial", "the sentence names a state", "test the transition"),
        Rule("R-003", "candidate", "never shown", "never shown"),
        Rule("R-004", "retired", "never shown either", "never shown"),
    )), d)
    inv = d / "inventory.md"
    inv.write_text("## Documented behaviours to cover\n- item", encoding="utf-8")
    monkeypatch.setenv(RULES_DOC_ENV, str(d))
    monkeypatch.setenv(INVENTORY_ENV, str(inv))
    prompt = _author_system_prompt()
    core_end = prompt.index("documents themselves are wasted budget.")
    rules_at = prompt.index("## Additional rules for this suite")
    inv_at = prompt.index("## Documented behaviours to cover")
    assert core_end < rules_at < inv_at
    assert "R-001" not in prompt and "test both sides of it" in prompt and "assert f(0) == 0" in prompt
    assert "test the transition" in prompt
    assert "never shown" not in prompt


def test_rules_doc_rejects_bad_state_and_duplicates() -> None:
    d = Path(tempfile.mkdtemp())
    (d / "rules.yaml").write_text("rules:\n- {rule_id: R-1, state: bogus, trigger: t, rule: r}\n", encoding="utf-8")
    try:
        load_rules_doc(d)
        raise AssertionError("bad state accepted")
    except RulesDocError:
        pass
    (d / "rules.yaml").write_text(
        "rules:\n- {rule_id: R-1, state: trial, trigger: t, rule: r}\n- {rule_id: R-1, state: trial, trigger: t, rule: r}\n",
        encoding="utf-8")
    try:
        load_rules_doc(d)
        raise AssertionError("duplicate accepted")
    except RulesDocError:
        pass


def test_author_config_defaults_off_and_parses() -> None:
    assert EvolutionConfig.from_config({}).author == AuthorConfig()
    assert EvolutionConfig.from_config({"evolution": {"enabled": True}}).author.enabled is False
    cfg = EvolutionConfig.from_config({"evolution": {"enabled": True, "author": {
        "enabled": True, "inventory": True, "coverage": {"max_rounds": 3}, "accept": {"min_milestones": 7}}}})
    assert cfg.author.enabled and cfg.author.inventory
    assert cfg.author.coverage_max_rounds == 3 and cfg.author.accept_min_milestones == 7
    assert cfg.author.publish is False and cfg.author.require_human_approval is True
