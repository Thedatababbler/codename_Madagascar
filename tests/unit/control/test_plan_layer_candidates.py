"""Recompiling a milestone into a different shape, which no edit can reach.

The shapes worth searching over are the ones the edit layer cannot produce: a
solo milestone becoming implement-gate-repair, a chain becoming test-first. Each
needs a harness, a conditional edge and a slot behind it, and an edit that adds
them by hand grades the wrong agent. So these tests are mostly about what the
recompiled graph *keeps* — the same suite, the same acceptance, the same
namespace discipline — because that is what makes it comparable to its parent.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from orchestra.cli.validate_graph import build_compiler
from orchestra.control.fast_loop.plan_candidates import (
    PlanRecompileError,
    TemplateSwitch,
    milestone_draft_for,
    recompile_candidate,
    register_new_contracts,
)
from orchestra.ir.compiler import GraphCompilationError
from orchestra.ir.graph import OrchestraGraph, load_graph
from orchestra.ir.graph_invariants import assert_graph_invariants
from orchestra.ir.nodes import NodeKind
from orchestra.realbench.milestone_planner import parse_plan_payload
from orchestra.realbench.subgraph_builder import (
    materialize_milestone_subgraph,
    prepare_generated_root,
)
from orchestra.roles.pool import load_role_pool
from orchestra.roles.templates import (
    catalog_lines,
    default_templates,
    load_templates,
    parse_template,
)

#: Custody is only wired when the harness command says where the authored suite
#: goes, and the frozen suite is the thing a recompilation must not move.
SPEC_HARNESS = ["python", "check.py", "--spec-tests", "/tmp/frozen"]


def _pool():
    return load_role_pool("configs/roles")


def _compiled(
    template_id: str,
    agents: list[dict[str, str]],
    *,
    harness_command: list[str] | None = None,
) -> OrchestraGraph:
    plan = parse_plan_payload(
        {
            "milestones": [
                {
                    "milestone_id": f"m_{template_id}",
                    "title": "T",
                    "objective": "build the thing",
                    "risk_rationale": "downstream depends on it",
                    "gate_level": "implementation",
                    "template_id": template_id,
                    "acceptance": {"criteria": ["it works"]},
                    "agents": agents,
                }
            ]
        },
        max_agents=4,
    )
    root = prepare_generated_root(
        Path(tempfile.mkdtemp()), base_contracts_dir="configs/contracts"
    )
    path, _ = materialize_milestone_subgraph(
        generated_root=root,
        milestone=plan.milestones[0],
        agent_backend="codex_sdk",
        harness_command=harness_command or SPEC_HARNESS,
    )
    return load_graph(path)


def _solo() -> OrchestraGraph:
    return _compiled(
        "solo", [{"slot": "author", "role": "implementer", "mandate": "write it"}]
    )


def _roles(graph: OrchestraGraph) -> list[str]:
    pool = _pool()
    return [
        role.role_id
        for node in graph.nodes
        if node.node_kind is NodeKind.AGENT
        for role in [pool.role_for_node_id(node.node_id)]
        if role is not None
    ]


def test_a_solo_milestone_recompiles_into_implement_gate_repair() -> None:
    base = _solo()
    candidate = recompile_candidate(
        base_graph=base,
        switch=TemplateSwitch(
            template_id="gate_then_repair",
            slots={"repairer": "gate_repairer"},
            playbook_id="add_a_repair_pass",
        ),
        candidate_id="cand_gate",
    )

    assert candidate.graph.metadata["template_id"] == "gate_then_repair"
    assert _roles(candidate.graph) == ["implementer", "gate_repairer"]
    # The point of the shape: a probe grades the author, and the repairer only
    # becomes runnable behind a failing one.
    gate_report = [
        edge for edge in candidate.graph.edges if edge.destination_input == "gate_report"
    ]
    assert len(gate_report) == 1
    assert gate_report[0].condition is not None
    assert gate_report[0].condition.operator == "is_false"


def test_the_recompiled_graph_satisfies_the_invariants_by_construction() -> None:
    candidate = recompile_candidate(
        base_graph=_solo(),
        switch=TemplateSwitch(
            template_id="review_then_fix",
            slots={"reviewer": "contract_critic", "fixer": "gate_repairer"},
        ),
        candidate_id="cand_review",
    )
    assert_graph_invariants(
        candidate.graph, pool=_pool(), expected_template_id="review_then_fix"
    )


def test_the_candidate_is_graded_against_its_parents_frozen_suite() -> None:
    """Same command, so the same suite outside the workspace decides both scores."""
    base = _solo()
    candidate = recompile_candidate(
        base_graph=base,
        switch=TemplateSwitch(
            template_id="gate_then_repair", slots={"repairer": "gate_repairer"}
        ),
        candidate_id="cand_suite",
    )
    commands = {
        tuple(node.command)
        for graph in (base, candidate.graph)
        for node in graph.nodes
        if node.node_kind is NodeKind.HARNESS
    }
    assert commands == {tuple(SPEC_HARNESS)}


def test_the_candidate_does_not_overwrite_the_attempt_it_is_compared_against() -> None:
    base = _solo()
    before = Path(base.metadata["milestone_draft_path"]).read_text(encoding="utf-8")
    candidate = recompile_candidate(
        base_graph=base,
        switch=TemplateSwitch(
            template_id="gate_then_repair", slots={"repairer": "gate_repairer"}
        ),
        candidate_id="cand_ns",
    )
    assert Path(base.metadata["milestone_draft_path"]).read_text(encoding="utf-8") == before
    assert load_graph(_graph_path(base)).content_hash == base.content_hash

    contract_ids = {
        node.contract_id
        for node in candidate.graph.nodes
        if node.node_kind is NodeKind.AGENT
    }
    parent_ids = {
        node.contract_id for node in base.nodes if node.node_kind is NodeKind.AGENT
    }
    assert not (contract_ids & parent_ids)
    assert candidate.plan_recompile is not None
    assert candidate.plan_recompile.contract_namespace == "cand_ns"


def _graph_path(graph: OrchestraGraph) -> str:
    draft = Path(graph.metadata["milestone_draft_path"])
    return str(draft.with_name(draft.name.replace(".draft.json", ".yaml")))


def test_the_candidate_records_the_template_pair_and_the_slots_that_moved() -> None:
    candidate = recompile_candidate(
        base_graph=_solo(),
        switch=TemplateSwitch(
            template_id="review_then_fix",
            slots={"reviewer": "spec_auditor", "fixer": "gate_repairer"},
            playbook_id="review_before_the_gate",
        ),
        candidate_id="cand_record",
    )
    recompile = candidate.plan_recompile
    assert recompile is not None
    assert (recompile.parent_template_id, recompile.template_id) == (
        "solo",
        "review_then_fix",
    )
    assert recompile.slots == {
        "author": "implementer",
        "reviewer": "spec_auditor",
        "fixer": "gate_repairer",
    }
    # The author kept its role, so it is not part of the delta.
    assert recompile.changed_slots == ["reviewer", "fixer"]
    assert candidate.playbook_id == "review_before_the_gate"
    assert candidate.edits == []


def test_a_solo_milestone_can_be_reviewed_by_the_behaviour_critic() -> None:
    """The angle neither document-reading reviewer covers, and it has to be reachable.

    `spec_auditor` reads the design documents and `contract_critic` reads the
    frozen interfaces; both pass a milestone whose symbols all exist and all
    behave wrongly. A role nothing can host is dead configuration, so this pins
    the one slot a playbook can put it in today.
    """
    candidate = recompile_candidate(
        base_graph=_solo(),
        switch=TemplateSwitch(
            template_id="review_then_fix",
            slots={"reviewer": "behaviour_critic", "fixer": "gate_repairer"},
            playbook_id="pb_solo_to_review_fix",
        ),
        candidate_id="cand_behaviour",
    )
    assert _roles(candidate.graph) == ["implementer", "behaviour_critic", "gate_repairer"]

    critic = next(
        node
        for node in candidate.graph.nodes
        if node.node_id.endswith("_behaviour_critic")
    )
    # A reviewer produces no diff, and a backend demanding one would score its
    # correct behaviour a failure.
    assert critic.resolved_backend().require_git_diff is False
    assert_graph_invariants(
        candidate.graph, pool=_pool(), expected_template_id="review_then_fix"
    )


TEST_FIRST_AGENTS = [
    {"slot": "test_author", "role": "test_author", "mandate": "write the suite"},
    {"slot": "builder", "role": "implementer", "mandate": "build it"},
    {"slot": "repairer", "role": "gate_repairer", "mandate": "fix what the gate names"},
]


def _test_first() -> OrchestraGraph:
    return _compiled("test_first", TEST_FIRST_AGENTS)


def _diagnosed(candidate_id: str = "cand_diagnosed"):  # noqa: ANN202 - LocalCandidate
    return recompile_candidate(
        base_graph=_test_first(),
        switch=TemplateSwitch(
            template_id="test_first_diagnosed",
            slots={"critic": "behaviour_critic"},
            playbook_id="pb_tf_diagnose_before_repair",
        ),
        candidate_id=candidate_id,
    )


def test_test_first_recompiles_with_a_critic_between_the_gate_and_the_repairer() -> None:
    candidate = _diagnosed()
    assert _roles(candidate.graph) == [
        "test_author",
        "implementer",
        "behaviour_critic",
        "gate_repairer",
    ]
    assert candidate.plan_recompile is not None
    # Only the critic is new; the parent's three slots keep their roles.
    assert candidate.plan_recompile.changed_slots == ["critic"]
    assert_graph_invariants(
        candidate.graph, pool=_pool(), expected_template_id="test_first_diagnosed"
    )


def test_the_critic_costs_nothing_when_the_gate_passes() -> None:
    """Both halves of the repair pass wait on a report only a failure produces."""
    candidate = _diagnosed("cand_gated")
    behind_the_gate = {
        edge.destination_node: edge
        for edge in candidate.graph.edges
        if edge.destination_input == "gate_report"
    }
    critic = next(
        node.node_id
        for node in candidate.graph.nodes
        if node.node_id.endswith("_behaviour_critic")
    )
    repairer = next(
        node.node_id
        for node in candidate.graph.nodes
        if node.node_id.endswith("_gate_repairer")
    )
    assert set(behind_the_gate) == {critic, repairer}
    for edge in behind_the_gate.values():
        assert edge.condition is not None
        assert (edge.condition.source_field, edge.condition.operator) == (
            "passed",
            "is_false",
        )


def test_the_repairer_reads_the_critics_report_and_still_feeds_the_gate() -> None:
    candidate = _diagnosed("cand_chain")
    critic = next(
        node.node_id
        for node in candidate.graph.nodes
        if node.node_id.endswith("_behaviour_critic")
    )
    repairer = next(
        node.node_id
        for node in candidate.graph.nodes
        if node.node_id.endswith("_gate_repairer")
    )
    assert any(
        edge.source_node == critic and edge.destination_node == repairer
        for edge in candidate.graph.edges
    )
    # The terminal harness must grade the repairer, not the critic's empty diff.
    graded = {
        edge.source_node
        for edge in candidate.graph.edges
        if edge.destination_node == "repository_tests"
    }
    assert graded == {repairer}


def test_the_builder_still_cannot_reach_the_suite_after_recompiling() -> None:
    """The hard invariant of `test_first`, which a variant template could undo."""
    candidate = _diagnosed("cand_custody")
    author = next(
        node.node_id
        for node in candidate.graph.nodes
        if node.node_id.endswith("_test_author")
    )
    builder = next(
        node.node_id
        for node in candidate.graph.nodes
        if node.node_id.endswith("_implementer")
    )
    # No change edge carries the author's patch into the builder ...
    assert not any(
        edge.source_node == author and edge.destination_node == builder
        for edge in candidate.graph.edges
    )
    # ... and the builder waits on custody having moved the suite out.
    custody = [
        edge
        for edge in candidate.graph.edges
        if edge.destination_node == builder and edge.destination_input == "suite_custody"
    ]
    assert len(custody) == 1
    assert custody[0].source_node == "authored_suite_custody"


def test_a_shape_ending_in_a_reviewer_is_refused() -> None:
    """The compiler freezes the last agent, and a reviewer's diff is empty.

    No shipped template can end this way, which is exactly why the guard is
    worth having: the next one to be written for a playbook could, and the
    milestone would then be scored on a change nobody made.
    """
    audit_last = parse_template(
        {
            "template_id": "audit_last",
            "title": "Build, then audit and stop",
            "when_to_use": "nothing; this shape is a mistake",
            "slots": [
                {
                    "id": "author",
                    "default_role": "implementer",
                    "allowed_roles": ["implementer"],
                },
                {
                    "id": "auditor",
                    "default_role": "spec_auditor",
                    "allowed_roles": ["spec_auditor"],
                },
            ],
            "edges": [{"from": "author", "to": "auditor"}],
        },
        source="test",
    )
    with pytest.raises(PlanRecompileError, match="grade an empty diff"):
        recompile_candidate(
            base_graph=_solo(),
            switch=TemplateSwitch(template_id="audit_last", slots={}),
            candidate_id="cand_readonly_tail",
            templates={**default_templates(), "audit_last": audit_last},
        )


def test_the_variant_is_not_offered_to_the_planner() -> None:
    """It repairs a milestone that already failed; it does not plan one.

    Listing it would change how tasks are decomposed as well as how they are
    repaired, so a run comparing search against no search would move two things.
    """
    templates = load_templates("configs/subgraph_templates", pool=_pool())
    assert "test_first_diagnosed" in templates
    assert "test_first_double_repair" in templates
    assert "test_first_improve" in templates
    assert "test_first_quality_diagnosed" in templates
    catalogue = "\n".join(catalog_lines(templates))
    assert "test_first_diagnosed" not in catalogue
    assert "test_first_double_repair" not in catalogue
    assert "test_first_improve" not in catalogue
    assert "test_first_quality_diagnosed" not in catalogue
    assert "test_first" in catalogue


def test_a_slot_the_parent_already_had_keeps_its_mandate() -> None:
    base = _compiled(
        "solo", [{"slot": "author", "role": "implementer", "mandate": "port the parser"}]
    )
    candidate = recompile_candidate(
        base_graph=base,
        switch=TemplateSwitch(
            template_id="gate_then_repair", slots={"repairer": "gate_repairer"}
        ),
        candidate_id="cand_mandate",
    )
    draft = milestone_draft_for(candidate.graph)
    author = next(agent for agent in draft.agents if agent.slot_id == "author")
    assert author.mandate == "port the parser"


def test_a_slot_whose_role_changed_does_not_inherit_the_old_mandate() -> None:
    """A mandate was written for one role; carrying it across misdirects the new one."""
    base = _compiled(
        "solo", [{"slot": "author", "role": "implementer", "mandate": "port the parser"}]
    )
    candidate = recompile_candidate(
        base_graph=base,
        switch=TemplateSwitch(template_id="solo", slots={"author": "contract_author"}),
        candidate_id="cand_role_swap",
    )
    draft = milestone_draft_for(candidate.graph)
    author = next(agent for agent in draft.agents if agent.slot_id == "author")
    assert author.role == "contract_author"
    assert "port the parser" not in author.mandate


def test_a_switch_that_fills_no_repair_slot_behind_an_early_gate_is_refused() -> None:
    """Otherwise the shape collapses to its parent's under a new template name."""
    with pytest.raises(PlanRecompileError, match="nothing behind it"):
        recompile_candidate(
            base_graph=_solo(),
            switch=TemplateSwitch(template_id="gate_then_repair"),
            candidate_id="cand_hollow",
        )


def test_a_switch_that_changes_nothing_is_refused() -> None:
    with pytest.raises(PlanRecompileError, match="reproduce its parent"):
        recompile_candidate(
            base_graph=_solo(),
            switch=TemplateSwitch(template_id="solo"),
            candidate_id="cand_noop",
        )


def test_a_role_the_slot_does_not_allow_is_an_error_rather_than_a_fallback() -> None:
    """The planner degrades to the default here; a playbook is code and must not."""
    with pytest.raises(PlanRecompileError, match="does not accept"):
        recompile_candidate(
            base_graph=_solo(),
            switch=TemplateSwitch(
                template_id="review_then_fix", slots={"reviewer": "implementer"}
            ),
            candidate_id="cand_bad_role",
        )


def test_a_slot_the_template_does_not_have_is_an_error() -> None:
    with pytest.raises(PlanRecompileError, match="no slots"):
        recompile_candidate(
            base_graph=_solo(),
            switch=TemplateSwitch(template_id="solo", slots={"reviewer": "spec_auditor"}),
            candidate_id="cand_bad_slot",
        )


def test_the_candidate_compiles_once_its_new_contracts_are_registered() -> None:
    """The registry is read at construction, and a recompilation adds to it.

    Left stale, the candidate fails to compile and is recorded as a rejected
    design rather than as a compiler that had not looked at the directory again.
    """
    base = _solo()
    contracts_dir = str(Path(base.metadata["generated_root"]) / "contracts")
    compiler = build_compiler(contracts_dir)
    candidate = recompile_candidate(
        base_graph=base,
        switch=TemplateSwitch(
            template_id="gate_then_repair", slots={"repairer": "gate_repairer"}
        ),
        candidate_id="cand_compile",
    )

    with pytest.raises(GraphCompilationError):
        compiler.compile(candidate.graph)
    assert register_new_contracts(
        compiler=compiler, graph=candidate.graph, contracts_dir=contracts_dir
    )
    compiler.compile(candidate.graph)
    # Nothing to load the second time, and the parent never needed a reload.
    assert not register_new_contracts(
        compiler=compiler, graph=candidate.graph, contracts_dir=contracts_dir
    )
    assert not register_new_contracts(
        compiler=compiler, graph=base, contracts_dir=contracts_dir
    )


def test_register_new_contracts_updates_the_executor_registry_too() -> None:
    """A candidate that compiles still dies if the executor has the old map."""
    base = _solo()
    contracts_dir = str(Path(base.metadata["generated_root"]) / "contracts")
    compiler = build_compiler(contracts_dir)
    candidate = recompile_candidate(
        base_graph=base,
        switch=TemplateSwitch(
            template_id="gate_then_repair", slots={"repairer": "gate_repairer"}
        ),
        candidate_id="cand_executor",
    )
    executor_contracts = dict(compiler.contracts)
    named = {
        node.contract_id
        for node in candidate.graph.nodes
        if node.node_kind is NodeKind.AGENT and node.contract_id
    }
    assert not named <= set(executor_contracts)
    assert register_new_contracts(
        compiler=compiler,
        graph=candidate.graph,
        contracts_dir=contracts_dir,
        executor_contracts=executor_contracts,
    )
    assert named <= set(executor_contracts)
    assert named <= set(compiler.contracts)


def test_a_graph_compiled_by_another_route_says_it_cannot_be_recompiled() -> None:
    base = _solo()
    stripped = base.model_copy(
        update={
            "metadata": {
                k: v for k, v in base.metadata.items() if k != "milestone_draft_path"
            }
        }
    )
    with pytest.raises(PlanRecompileError, match="nothing to recompile from"):
        recompile_candidate(
            base_graph=stripped,
            switch=TemplateSwitch(
                template_id="gate_then_repair", slots={"repairer": "gate_repairer"}
            ),
            candidate_id="cand_no_draft",
        )


def test_candidate_checkpoints_are_scoped_to_their_milestone() -> None:
    """Two milestones draft the same candidate ids, so the key needs the subtask.

    Without it the second searching milestone finds the first one's checkpoint
    under its own name, the store calls that config drift, and every candidate
    dies in milliseconds — the milestone then commits its incumbent because the
    search it paid for produced nothing to compare against.
    """
    from orchestra.control.fast_loop.controller import candidate_task_id

    first = candidate_task_id("rb_imapclient", "freeze_shared_api", "cand_feedback")
    second = candidate_task_id("rb_imapclient", "implement_behaviour", "cand_feedback")

    assert first != second
    assert "freeze_shared_api" in first and "implement_behaviour" in second


def test_every_planner_selectable_template_carries_the_suite_author_slot() -> None:
    """The authored suite is the behavioural measurement, so no shape the
    planner may choose can lack the slot that authors it -- a milestone without
    one saturates behaviour at 1.0 and the quality search goes blind
    (EXP-20260810-05). `solo` is withdrawn from the catalogue for exactly that
    reason, and stays compilable only for frozen-plan replays.
    """
    templates = default_templates()
    for template in templates.values():
        if not template.planner_selectable:
            continue
        assert any(s.slot_id == "test_author" for s in template.slots), (
            template.template_id
        )
    assert not templates["solo"].planner_selectable
    lines = "\n".join(catalog_lines(templates))
    assert "`solo`" not in lines


def test_recompiling_from_solo_does_not_author_a_new_suite() -> None:
    """The yardstick is fixed when the milestone freezes; a search must not
    re-author it. The target template's test_author slot is optional precisely
    so that _fill_slots skips it when the parent had none -- a synthesised
    suite author would take custody of a fresh suite and grade this candidate
    against different tests than its incumbent.
    """
    candidate = recompile_candidate(
        base_graph=_solo(),
        switch=TemplateSwitch(
            template_id="gate_then_repair", slots={"repairer": "gate_repairer"}
        ),
        candidate_id="cand_no_new_suite",
    )
    assert "test_author" not in candidate.plan_recompile.slots
    assert _roles(candidate.graph) == ["implementer", "gate_repairer"]
