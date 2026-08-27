"""The playbook generator drafts from the table and leaves the atomic one alone."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from orchestra.control.fast_loop.candidate_generator import DesignSearchCandidateGenerator
from orchestra.control.fast_loop.controller import FastLoopController
from orchestra.control.fast_loop.playbook_generator import PlaybookCandidateGenerator
from orchestra.control.fast_loop.playbooks import FailureClass
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
    assert diagnosed.edits == []
    roles = [
        role.role_id
        for node in diagnosed.graph.nodes
        if node.node_kind is NodeKind.AGENT
        for role in [_pool().role_for_node_id(node.node_id)]
        if role is not None
    ]
    assert roles == ["test_author", "implementer", "behaviour_critic", "gate_repairer"]


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


@pytest.mark.parametrize("candidates", [0, 1, 2, 3, 6])
def test_the_generator_never_exceeds_its_budget(candidates: int) -> None:
    built = _generate(
        _test_first(),
        candidates=candidates,
        failures=["test_a"],
        failure_class=FailureClass.FUNCTIONAL,
    )
    assert len(built) <= candidates
