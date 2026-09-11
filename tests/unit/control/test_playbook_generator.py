"""The playbook generator drafts from the table and leaves the atomic one alone."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from orchestra.backends.codex_sdk import CodexSDKBackend
from orchestra.cli.validate_graph import build_compiler
from orchestra.control.fast_loop.candidate_generator import DesignSearchCandidateGenerator
from orchestra.control.fast_loop.controller import FastLoopController
from orchestra.control.fast_loop.playbook_generator import PlaybookCandidateGenerator
from orchestra.control.fast_loop.playbooks import FailureClass, SearchReason
from orchestra.control.fast_loop.quality_trigger import (
    build_incumbent_record,
    quality_search_diagnosis,
)
from orchestra.control.fast_loop.schemas import FailureDiagnosis, FastLoopBudget
from orchestra.control.task_state import SubtaskFailureReason
from orchestra.ir.graph import OrchestraGraph, load_graph
from orchestra.ir.nodes import NodeKind
from orchestra.realbench.milestone_planner import parse_plan_payload
from orchestra.realbench.subgraph_builder import (
    materialize_milestone_subgraph,
    prepare_generated_root,
)
from orchestra.roles.pool import load_role_pool

SPEC_HARNESS = ["python", "check.py", "--spec-tests", "/tmp/frozen"]


def _pool():
    return load_role_pool("configs/roles")


def _compiled(template_id: str, agents: list[dict[str, str]]) -> OrchestraGraph:
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
        harness_command=SPEC_HARNESS,
    )
    return load_graph(path)


def _test_first() -> OrchestraGraph:
    return _compiled(
        "test_first",
        [
            {"slot": "test_author", "role": "test_author", "mandate": "write the suite"},
            {"slot": "builder", "role": "implementer", "mandate": "build it"},
            {"slot": "repairer", "role": "gate_repairer", "mandate": "fix the gate"},
        ],
    )


def _solo() -> OrchestraGraph:
    return _compiled(
        "solo", [{"slot": "author", "role": "implementer", "mandate": "write it"}]
    )


def _agent(graph: OrchestraGraph, role_suffix: str) -> str:
    return next(
        node.node_id
        for node in graph.nodes
        if node.node_kind is NodeKind.AGENT and node.node_id.endswith(role_suffix)
    )


def _diagnosis(
    graph: OrchestraGraph,
    *,
    stage: str = "spec_tests",
    failures: list[str] | None = None,
    failure_class: str = "",
    infra: bool = False,
    role_suffix: str = "_implementer",
) -> FailureDiagnosis:
    return FailureDiagnosis(
        reason=SubtaskFailureReason.INFRA if infra else SubtaskFailureReason.HARNESS,
        retryable=True,
        concise_feedback="FAIL spec_tests: test_merge_keeps_the_later_timestamp",
        primary_failed_node_id=_agent(graph, role_suffix),
        failed_node_ids=[_agent(graph, role_suffix)],
        furthest_stage=stage,
        failure_class=failure_class,
        behaviour_failures=list(failures or []),
        infrastructure_related=infra,
    )


def _generate(
    graph: OrchestraGraph,
    *,
    candidates: int = 3,
    **diagnosis_kwargs,
) -> list:
    return PlaybookCandidateGenerator(role_pool=_pool()).generate(
        graph=graph,
        diagnosis=_diagnosis(graph, **diagnosis_kwargs),
        budget=FastLoopBudget(max_candidates=candidates, max_total_backend_calls=99),
        capabilities={},
    )


def test_the_first_candidate_is_the_feedback_anchor() -> None:
    built = _generate(_test_first(), failures=["test_merge_keeps_the_later_timestamp"])
    assert built[0].candidate_id == "cand_feedback"
    assert built[0].playbook_id == ""
    assert {e.type for e in built[0].edits} == {"prompt_feedback", "session_policy"}


def test_test_first_functional_puts_the_failure_list_on_the_repairer() -> None:
    graph = _test_first()
    built = _generate(
        graph,
        failures=["test_merge_keeps_the_later_timestamp"],
        failure_class=FailureClass.FUNCTIONAL,
    )
    ids = [c.candidate_id for c in built]
    assert ids[:2] == ["cand_feedback", "cand_pb_tf_failures_to_repairer"]
    repairer = _agent(graph, "_gate_repairer")
    listed = next(c for c in built if c.playbook_id == "pb_tf_failures_to_repairer")
    assert any(
        e.type == "prompt_feedback"
        and e.node_id == repairer
        and "test_merge_keeps_the_later_timestamp" in e.feedback
        for e in listed.edits
    )


def test_test_first_recompiles_into_the_diagnosed_shape() -> None:
    graph = _test_first()
    built = _generate(
        graph,
        candidates=3,
        failures=["test_a"],
        failure_class=FailureClass.FUNCTIONAL,
    )
    diagnosed = next(c for c in built if c.playbook_id == "pb_tf_diagnose_before_repair")
    assert diagnosed.plan_recompile is not None
    assert diagnosed.plan_recompile.template_id == "test_first_diagnosed"
    # The critic was added to read evidence, so it is handed the names -- and
    # so is the repairer it reports to. A shape row used to carry no feedback.
    told = {
        e.node_id
        for e in diagnosed.edits
        if e.type == "prompt_feedback" and "test_a" in e.feedback
    }
    assert told == {_agent(diagnosed.graph, "_behaviour_critic"), _agent(diagnosed.graph, "_gate_repairer")}
    roles = [
        role.role_id
        for node in diagnosed.graph.nodes
        if node.node_kind is NodeKind.AGENT
        for role in [_pool().role_for_node_id(node.node_id)]
        if role is not None
    ]
    assert roles == ["test_author", "implementer", "behaviour_critic", "gate_repairer"]


def test_test_first_recompiles_into_two_repair_rounds() -> None:
    graph = _test_first()
    built = _generate(
        graph,
        candidates=4,
        failures=["test_a"],
        failure_class=FailureClass.FUNCTIONAL,
    )
    doubled = next(c for c in built if c.playbook_id == "pb_tf_second_repairer")
    assert doubled.plan_recompile is not None
    assert doubled.plan_recompile.template_id == "test_first_double_repair"
    roles = [
        role.role_id
        for node in doubled.graph.nodes
        if node.node_kind is NodeKind.AGENT
        for role in [_pool().role_for_node_id(node.node_id)]
        if role is not None
    ]
    assert roles == ["test_author", "implementer", "gate_repairer", "gate_repairer"]
    behind_the_gate = {
        edge.destination_node
        for edge in doubled.graph.edges
        if edge.destination_input == "gate_report"
    }
    assert len(behind_the_gate) == 2


def test_a_budget_class_does_not_pay_for_the_critic() -> None:
    built = _generate(
        _test_first(),
        stage="imports",
        failure_class=FailureClass.BUDGET,
        role_suffix="_implementer",
    )
    assert [c.playbook_id for c in built] == ["", "pb_tf_builder_budget"]


def test_solo_functional_offers_a_conditional_repair_pass() -> None:
    built = _generate(_solo(), failure_class=FailureClass.FUNCTIONAL, role_suffix="_implementer")
    assert built[1].playbook_id == "pb_solo_to_gate_repair"
    assert built[1].plan_recompile is not None
    assert built[1].plan_recompile.template_id == "gate_then_repair"


def test_without_named_failures_the_repairer_playbook_is_skipped() -> None:
    built = _generate(_test_first(), failures=[], failure_class=FailureClass.FUNCTIONAL)
    assert "pb_tf_failures_to_repairer" not in {c.playbook_id for c in built}
    assert "pb_tf_diagnose_before_repair" in {c.playbook_id for c in built}


def test_infrastructure_failures_are_not_searched() -> None:
    graph = _test_first()
    built = _generate(graph, infra=True)
    assert built == []


def test_the_atomic_generator_is_untouched() -> None:
    """The old path still drafts add-a-role, not a playbook switch."""
    graph = _test_first()
    built = DesignSearchCandidateGenerator(role_pool=_pool()).generate(
        graph=graph,
        diagnosis=_diagnosis(graph, failures=["test_a"], stage="tests"),
        budget=FastLoopBudget(max_candidates=2, max_total_backend_calls=99),
        capabilities={},
    )
    assert [c.candidate_id for c in built] == [
        "cand_feedback",
        "cand_add_test_driven_implementer",
    ]
    assert all(c.playbook_id == "" for c in built)


def test_playbook_search_and_design_search_cannot_both_be_on() -> None:
    with pytest.raises(ValueError, match="mutually exclusive"):
        FastLoopController(
            runtime=None,  # type: ignore[arg-type]
            artifact_store=None,  # type: ignore[arg-type]
            task_checkpoint_store=None,  # type: ignore[arg-type]
            design_search=True,
            playbook_search=True,
        )


def test_quality_search_drafts_from_the_quality_table_not_the_failure_one() -> None:
    """A passing-but-poor milestone must not pick repairers behind a failing gate."""
    graph = _test_first()
    incumbent = build_incumbent_record(
        attempt_id=1,
        graph_hash=graph.content_hash,
        harness_score=0.928571,
        behaviour_score=0.7619,
        behaviour_failures=["test_starttls_requires_server_capability"],
        furthest_stage="spec_tests",
    )
    diagnosis = quality_search_diagnosis(incumbent, graph)
    built = PlaybookCandidateGenerator(role_pool=_pool()).generate(
        graph=graph,
        diagnosis=diagnosis,
        budget=FastLoopBudget(max_candidates=3, max_total_backend_calls=99),
        capabilities={},
        search_reason=SearchReason.QUALITY,
    )
    ids = [c.playbook_id for c in built]
    assert ids == [
        "",
        "pb_q_continue_improve",
        "pb_tf_q_improve_after_gate",
    ]
    assert all(
        c.playbook_id not in {
            "pb_tf_failures_to_repairer",
            "pb_tf_diagnose_before_repair",
            "pb_tf_second_repairer",
        }
        for c in built
    )
    # No *playbook* writes to the parent builder any more: the only row that
    # did lost to the anchor every time it ran. The anchor still does -- that
    # is what it is, and what the playbooks are measured against.
    builder = _agent(graph, "_implementer")
    assert not any(
        e.type == "prompt_feedback" and e.node_id == builder
        for c in built
        if c.playbook_id
        for e in c.edits
    )
    improved = next(c for c in built if c.playbook_id == "pb_tf_q_improve_after_gate")
    assert improved.plan_recompile is not None
    assert improved.plan_recompile.template_id == "test_first_improve"
    roles = [
        role.role_id
        for node in improved.graph.nodes
        if node.node_kind is NodeKind.AGENT
        for role in [_pool().role_for_node_id(node.node_id)]
        if role is not None
    ]
    # The optional repairer slot now rides along: it is the safety net behind
    # the post-improver probe, inherited from the parent plan by slot name.
    assert roles == ["test_author", "implementer", "edge_case_hardener", "gate_repairer"]
    improver = _agent(improved.graph, "_edge_case_hardener")
    assert any(
        e.type == "prompt_feedback"
        and e.node_id == improver
        and "test_starttls_requires_server_capability" in e.feedback
        and "gate passed" in e.feedback
        for e in improved.edits
    )


def test_quality_prompt_says_when_the_gate_named_only_a_subset() -> None:
    graph = _test_first()
    incumbent = build_incumbent_record(
        attempt_id=1,
        graph_hash=graph.content_hash,
        harness_score=0.74,
        behaviour_score=0.14,
        behaviour_failures=[
            "imapclient.tests.TestIMAPClientBasics::test_context_manager_calls_logout_on_exit",
            "imapclient.tests.TestIMAPClientBasics::test_folder_status",
            "imapclient.tests.TestIMAPClientBasics::test_idle",
        ],
        behaviour_total=42,
        furthest_stage="spec_tests",
    )
    built = PlaybookCandidateGenerator(role_pool=_pool()).generate(
        graph=graph,
        diagnosis=quality_search_diagnosis(incumbent, graph),
        budget=FastLoopBudget(max_candidates=2, max_total_backend_calls=99),
        capabilities={},
        search_reason=SearchReason.QUALITY,
    )
    listed = next(c for c in built if c.playbook_id == "pb_q_continue_improve")
    feedback = next(
        e.feedback
        for e in listed.edits
        if e.type == "prompt_feedback" and "these behaviours still fail" in e.feedback
    )
    assert "42 tests and 6 passed" in feedback
    assert "named 3 of the 36 failures" in feedback
    assert "TestIMAPClientBasics::test_idle" in feedback
    assert "imapclient.tests." not in feedback


def test_quality_search_on_improve_does_not_reswitch_the_same_shape() -> None:
    graph = _compiled(
        "test_first_improve",
        [
            {"slot": "test_author", "role": "test_author", "mandate": "write the suite"},
            {"slot": "builder", "role": "implementer", "mandate": "build it"},
            {"slot": "improver", "role": "edge_case_hardener", "mandate": "harden leaks"},
        ],
    )
    incumbent = build_incumbent_record(
        attempt_id=1,
        graph_hash=graph.content_hash,
        harness_score=0.74,
        behaviour_score=0.14,
        behaviour_failures=["TestIMAPClientBasics::test_idle"],
        behaviour_total=42,
        furthest_stage="spec_tests",
    )
    built = PlaybookCandidateGenerator(role_pool=_pool()).generate(
        graph=graph,
        diagnosis=quality_search_diagnosis(incumbent, graph),
        budget=FastLoopBudget(max_candidates=3, max_total_backend_calls=99),
        capabilities={},
        search_reason=SearchReason.QUALITY,
    )
    assert [c.playbook_id for c in built] == [
        "",
        "pb_q_continue_improve",
        "pb_tf_q_failures_to_improver",
    ]
    improver = _agent(graph, "_edge_case_hardener")
    named = next(c for c in built if c.playbook_id == "pb_tf_q_failures_to_improver")
    assert any(
        e.type == "prompt_feedback" and e.node_id == improver and "test_idle" in e.feedback
        for e in named.edits
    )


def test_quality_diagnose_then_improve_tells_both_new_slots() -> None:
    graph = _test_first()
    incumbent = build_incumbent_record(
        attempt_id=1,
        graph_hash=graph.content_hash,
        harness_score=0.74,
        behaviour_score=0.14,
        behaviour_failures=["TestIMAPClientBasics::test_idle"],
        behaviour_total=42,
        furthest_stage="spec_tests",
    )
    built = PlaybookCandidateGenerator(role_pool=_pool()).generate(
        graph=graph,
        diagnosis=quality_search_diagnosis(incumbent, graph),
        budget=FastLoopBudget(max_candidates=4, max_total_backend_calls=99),
        capabilities={},
        search_reason=SearchReason.QUALITY,
    )
    assert [c.playbook_id for c in built] == [
        "",
        "pb_q_continue_improve",
        "pb_tf_q_improve_after_gate",
        "pb_tf_q_diagnose_then_improve",
    ]
    diagnosed = next(c for c in built if c.playbook_id == "pb_tf_q_diagnose_then_improve")
    assert diagnosed.plan_recompile is not None
    assert diagnosed.plan_recompile.template_id == "test_first_quality_diagnosed"
    critic = _agent(diagnosed.graph, "_behaviour_critic")
    improver = _agent(diagnosed.graph, "_edge_case_hardener")
    assert any(
        e.type == "prompt_feedback" and e.node_id == critic and "test_idle" in e.feedback
        for e in diagnosed.edits
    )
    assert any(
        e.type == "prompt_feedback" and e.node_id == improver and "test_idle" in e.feedback
        for e in diagnosed.edits
    )


@pytest.mark.parametrize("candidates", [0, 1, 2, 3, 6])
def test_the_generator_never_exceeds_its_budget(candidates: int) -> None:
    built = _generate(
        _test_first(),
        candidates=candidates,
        failures=["test_a"],
        failure_class=FailureClass.FUNCTIONAL,
    )
    assert len(built) <= candidates


def test_a_recompiled_candidate_compiles_inside_the_generator() -> None:
    """The validation compile runs before the controller ever sees the candidate.

    ``recompile_candidate`` writes one contract per agent of the new shape, and
    ``_annotate_recompile`` then binds the failure names onto that graph — which
    compiles it. The controller registers the new contracts too, but that call
    happens later, so relying on it alone left the generator compiling a graph
    naming contracts its registry had never read. Every other test in this file
    constructs the generator without a compiler, where ``apply_local_edits``
    skips the compile entirely and the gap is invisible.
    """
    graph = _test_first()
    contracts_dir = str(Path(graph.metadata["generated_root"]) / "contracts")
    incumbent = build_incumbent_record(
        attempt_id=1,
        graph_hash=graph.content_hash,
        harness_score=0.928571,
        behaviour_score=0.7619,
        behaviour_failures=["test_starttls_requires_server_capability"],
        furthest_stage="spec_tests",
    )
    diagnosis = quality_search_diagnosis(incumbent, graph)
    built = PlaybookCandidateGenerator(
        role_pool=_pool(),
        compiler=build_compiler(contracts_dir),
        contracts_dir=contracts_dir,
    ).generate(
        graph=graph,
        diagnosis=diagnosis,
        budget=FastLoopBudget(max_candidates=3, max_total_backend_calls=99),
        capabilities={"codex_sdk": CodexSDKBackend().capabilities},
        search_reason=SearchReason.QUALITY,
    )

    improved = next(c for c in built if c.playbook_id == "pb_tf_q_improve_after_gate")
    assert not improved.compatibility_rejected
    assert improved.plan_recompile is not None
    assert improved.plan_recompile.template_id == "test_first_improve"


def test_an_unresolvable_candidate_is_rejected_not_raised() -> None:
    """A candidate that cannot compile must not take the milestone down with it.

    The generator runs inside ``FastLoopController.run``; an exception escaping
    it aborts the task before any milestone is scored, which is a far worse
    outcome than one design being dropped from the draft list.
    """
    graph = _test_first()
    incumbent = build_incumbent_record(
        attempt_id=1,
        graph_hash=graph.content_hash,
        harness_score=0.928571,
        behaviour_score=0.7619,
        behaviour_failures=["test_starttls_requires_server_capability"],
        furthest_stage="spec_tests",
    )
    diagnosis = quality_search_diagnosis(incumbent, graph)
    # A compiler that knows the parent's contracts, and a directory that will
    # never yield the recompiled ones: exactly the stale registry the run hit.
    built = PlaybookCandidateGenerator(
        role_pool=_pool(),
        compiler=build_compiler(str(Path(graph.metadata["generated_root"]) / "contracts")),
        contracts_dir=tempfile.mkdtemp(),
    ).generate(
        graph=graph,
        diagnosis=diagnosis,
        budget=FastLoopBudget(max_candidates=3, max_total_backend_calls=99),
        capabilities={"codex_sdk": CodexSDKBackend().capabilities},
        search_reason=SearchReason.QUALITY,
    )

    improved = next(c for c in built if c.playbook_id == "pb_tf_q_improve_after_gate")
    assert improved.compatibility_rejected
    assert "Missing contract" in (improved.rejection_message or "")


def test_the_improve_shape_probes_the_improver_and_repairs_its_failures() -> None:
    """The early gate sits after the improver, with a conditional repairer.

    Without this the improve shape had no gate between the writers and the
    final harness, so a builder omission -- two dropped contract symbols, both
    official probes of 2026-08-31 -- sailed uncaught into the final gate and
    scored the whole candidate zero. The probe freezes the improver's change
    on a pass, so the repairer costs nothing on the happy path.
    """
    graph = _test_first()
    incumbent = build_incumbent_record(
        attempt_id=1,
        graph_hash=graph.content_hash,
        harness_score=0.928571,
        behaviour_score=0.7619,
        behaviour_failures=["test_starttls_requires_server_capability"],
        furthest_stage="spec_tests",
    )
    built = PlaybookCandidateGenerator(role_pool=_pool()).generate(
        graph=graph,
        diagnosis=quality_search_diagnosis(incumbent, graph),
        budget=FastLoopBudget(max_candidates=3, max_total_backend_calls=99),
        capabilities={},
        search_reason=SearchReason.QUALITY,
    )
    improved = next(c for c in built if c.playbook_id == "pb_tf_q_improve_after_gate")
    g = improved.graph
    improver = _agent(g, "_edge_case_hardener")
    repairer = _agent(g, "_gate_repairer")
    assert any(n.node_id == "repository_tests_probe" for n in g.nodes)
    edges = {e.edge_id: e for e in g.edges}
    # The probe grades the improver's change, not the builder's.
    assert edges["early_agent_to_probe"].source_node == improver
    # The repairer only becomes runnable behind a failing probe...
    fail_edge = next(
        e for e in g.edges
        if e.source_node == "repository_tests_probe" and e.destination_node == repairer
    )
    assert fail_edge.condition is not None
    # ...and a passing probe freezes the improver's change directly.
    assert edges["probe_pass_to_freeze"].destination_node == "freeze_change"
    assert edges["early_agent_to_freeze"].source_node == improver


def test_anchor_repeat_fills_every_slot_with_the_same_design() -> None:
    """The control arm: k anchors, one design, distinct identities.

    Distinct candidate ids matter beyond bookkeeping -- workspaces and
    checkpoints key on them, so identical ids would collide the way the
    milestone-collision bug did. The designs must be byte-identical (same
    edits, same session policy) or the arm stops measuring noise.
    """
    graph = _test_first()
    incumbent = build_incumbent_record(
        attempt_id=1,
        graph_hash=graph.content_hash,
        harness_score=0.928571,
        behaviour_score=0.7619,
        behaviour_failures=["test_starttls_requires_server_capability"],
        furthest_stage="spec_tests",
    )
    built = PlaybookCandidateGenerator(role_pool=_pool(), anchor_repeat=True).generate(
        graph=graph,
        diagnosis=quality_search_diagnosis(incumbent, graph),
        budget=FastLoopBudget(max_candidates=3, max_total_backend_calls=99),
        capabilities={},
        search_reason=SearchReason.QUALITY,
    )
    assert [c.candidate_id for c in built] == [
        "cand_feedback",
        "cand_feedback_r2",
        "cand_feedback_r3",
    ]
    assert all(c.playbook_id == "" for c in built)
    baseline = [(e.type, e.node_id, getattr(e, "feedback", None)) for e in built[0].edits]
    for cand in built[1:]:
        assert [
            (e.type, e.node_id, getattr(e, "feedback", None)) for e in cand.edits
        ] == baseline


def test_the_three_search_modes_are_mutually_exclusive() -> None:
    with pytest.raises(ValueError, match="mutually exclusive"):
        FastLoopController(
            runtime=None,
            artifact_store=None,
            task_checkpoint_store=None,
            playbook_search=True,
            anchor_search=True,
        )


def test_continuation_row_exists_on_every_authored_suite_shape() -> None:
    """NL2Repo plans compile to review_then_fix and chain; the continuation must be offered there too."""
    from orchestra.control.fast_loop.node_resample import writer_steps  # noqa: F401  (import check only)

    shapes = {
        "review_then_fix": [
            {"slot": "test_author", "role": "test_author", "mandate": "write the suite"},
            {"slot": "author", "role": "contract_author", "mandate": "author"},
            {"slot": "reviewer", "role": "spec_auditor", "mandate": "review"},
            {"slot": "fixer", "role": "gate_repairer", "mandate": "fix"},
        ],
        "chain": [
            {"slot": "test_author", "role": "test_author", "mandate": "write the suite"},
            {"slot": "first", "role": "implementer", "mandate": "first"},
            {"slot": "second", "role": "implementer", "mandate": "second"},
            {"slot": "third", "role": "integrator", "mandate": "third"},
        ],
    }
    for template_id, agents in shapes.items():
        graph = _compiled(template_id, agents)
        incumbent = build_incumbent_record(
            attempt_id=1, graph_hash=graph.content_hash, harness_score=0.8, behaviour_score=0.5,
            behaviour_failures=["pkg.tests.TestX::test_a", "pkg.tests.TestX::test_b"],
            behaviour_total=4, furthest_stage="spec_tests",
        )
        built = PlaybookCandidateGenerator(role_pool=_pool()).generate(
            graph=graph, diagnosis=quality_search_diagnosis(incumbent, graph),
            budget=FastLoopBudget(max_candidates=2, max_total_backend_calls=99),
            capabilities={}, search_reason=SearchReason.QUALITY,
        )
        ids = [c.playbook_id for c in built if c.playbook_id]
        assert ids and ids[0] == "pb_q_continue_improve", (template_id, ids)
        cont = built[[c.playbook_id for c in built].index("pb_q_continue_improve")]
        # capabilities={} rejects every candidate on the backend check; the recompile itself must have worked
        assert cont.continue_from_incumbent and "capabilities" in (cont.rejection_message or "capabilities"), (template_id, cont.rejection_message)
        assert cont.plan_recompile is not None and cont.plan_recompile.template_id == "continuation"
        assert any(node.node_kind is NodeKind.AGENT for node in cont.graph.nodes)
