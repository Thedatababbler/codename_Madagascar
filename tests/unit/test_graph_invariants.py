"""Structural invariants, and the edit-layer defects they were written to catch.

The positive tests matter as much as the negative ones: a validator that rejects
the graphs the planner actually produces is worse than no validator, because it
would block the plan layer that these invariants exist to protect.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
import yaml

from orchestra.control.fast_loop.edit_engine import LocalEditError, apply_local_edits
from orchestra.control.fast_loop.schemas import AddRoleAgentEdit
from orchestra.ir.edges import EdgeCondition, EdgeSpec
from orchestra.ir.graph import OrchestraGraph
from orchestra.ir.graph_invariants import (
    GraphInvariantError,
    assert_graph_invariants,
    check_graph_invariants,
)
from orchestra.realbench.milestone_planner import parse_plan_payload
from orchestra.realbench.subgraph_builder import (
    materialize_milestone_subgraph,
    prepare_generated_root,
)
from orchestra.roles.pool import load_role_pool

#: Custody is only wired when the harness command can be told where to put the
#: authored suite, so the `test_first` shape needs this command to be complete.
SPEC_HARNESS = ["python", "check.py", "--spec-tests", "/tmp/frozen"]

TEMPLATE_AGENTS = {
    "solo": [{"slot": "author", "role": "implementer", "mandate": "a"}],
    "chain": [
        {"slot": "first", "role": "contract_author", "mandate": "a"},
        {"slot": "second", "role": "implementer", "mandate": "b"},
    ],
    "gate_then_repair": [
        {"slot": "author", "role": "implementer", "mandate": "a"},
        {"slot": "repairer", "role": "gate_repairer", "mandate": "b"},
    ],
    "review_then_fix": [
        {"slot": "author", "role": "implementer", "mandate": "a"},
        {"slot": "reviewer", "role": "contract_critic", "mandate": "b"},
        {"slot": "fixer", "role": "gate_repairer", "mandate": "c"},
    ],
    "parallel_audit": [
        {"slot": "author", "role": "implementer", "mandate": "a"},
        {"slot": "spec_review", "role": "spec_auditor", "mandate": "b"},
        {"slot": "contract_review", "role": "contract_critic", "mandate": "c"},
        {"slot": "fixer", "role": "gate_repairer", "mandate": "d"},
    ],
    "test_first": [
        {"slot": "test_author", "role": "test_author", "mandate": "a"},
        {"slot": "builder", "role": "implementer", "mandate": "b"},
        {"slot": "repairer", "role": "gate_repairer", "mandate": "c"},
    ],
}


def _pool():
    return load_role_pool("configs/roles")


def _graph(template_id: str, *, harness_command: list[str] | None = None) -> OrchestraGraph:
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
                    "agents": TEMPLATE_AGENTS[template_id],
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
        harness_command=harness_command or ["python", "check.py"],
    )
    return OrchestraGraph(**yaml.safe_load(Path(path).read_text(encoding="utf-8")))


def _node_id(graph: OrchestraGraph, needle: str) -> str:
    return next(node.node_id for node in graph.nodes if needle in node.node_id)


@pytest.mark.parametrize("template_id", sorted(TEMPLATE_AGENTS))
def test_every_shipped_template_satisfies_every_invariant(template_id: str) -> None:
    graph = _graph(template_id, harness_command=SPEC_HARNESS)
    assert_graph_invariants(
        graph, pool=_pool(), expected_template_id=template_id
    )


def test_custody_is_not_counted_as_a_gate_the_milestone_pays_for() -> None:
    """A `test_first` milestone that dropped its repair slot is legal.

    Custody reads the author's change and reports like a gate does, so counting
    it as one made this shape look like it ran an early probe with nothing behind
    it. The template fixtures all fill the repair slot, which hid it.
    """
    plan = parse_plan_payload(
        {
            "milestones": [
                {
                    "milestone_id": "m_no_repairer",
                    "title": "T",
                    "objective": "build the thing",
                    "risk_rationale": "downstream depends on it",
                    "gate_level": "implementation",
                    "template_id": "test_first",
                    "agents": TEMPLATE_AGENTS["test_first"][:2],
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
    graph = OrchestraGraph(**yaml.safe_load(Path(path).read_text(encoding="utf-8")))
    assert any(node.node_id == "authored_suite_custody" for node in graph.nodes)
    assert_graph_invariants(graph, pool=_pool(), expected_template_id="test_first")


def test_a_read_only_agent_spliced_after_the_builder_makes_the_gate_score_nothing() -> None:
    """The defect `add_role_agent` has today, pinned.

    It re-sources *every* outgoing edge of its anchor, so the early probe ends up
    grading the inserted node. A read-only role produces no diff, so the
    behavioural score describes the reviewer's non-change rather than the
    builder's work — and nothing raises, which is why this needs a validator
    rather than a comment.
    """
    graph = _graph("test_first", harness_command=SPEC_HARNESS)
    builder = _node_id(graph, "builder")

    edited = apply_local_edits(
        graph,
        [AddRoleAgentEdit(role_id="spec_auditor", after_node_id=builder)],
        role_pool=_pool(),
        # The edit is still defective; what changed is that the pipeline now
        # refuses its result. Turned off here so the defect stays observable.
        check_invariants=False,
    )

    violations = check_graph_invariants(edited, pool=_pool())
    assert [v.invariant for v in violations] == ["gate_scores_editing_agent"]
    assert "empty diff" in violations[0].message


def test_the_edit_pipeline_refuses_that_candidate_rather_than_running_it() -> None:
    """What the validator buys: the defect above costs a rejection, not a run."""
    graph = _graph("test_first", harness_command=SPEC_HARNESS)
    builder = _node_id(graph, "builder")

    with pytest.raises(LocalEditError, match="empty diff"):
        apply_local_edits(
            graph,
            [AddRoleAgentEdit(role_id="spec_auditor", after_node_id=builder)],
            role_pool=_pool(),
        )


def test_the_repairs_branch_survives_that_edit_even_though_the_score_does_not() -> None:
    """The gate report comes from the probe, so the repair branch is not the casualty.

    Recorded because the opposite was assumed while designing the playbooks: the
    conditional edge feeding a repairer is sourced from the harness, not from the
    agent being spliced, so inserting a node does not disable the branch. What it
    damages is the artifact the gate reads.
    """
    graph = _graph("test_first", harness_command=SPEC_HARNESS)
    builder = _node_id(graph, "builder")
    repairer = _node_id(graph, "gate_repairer")

    edited = apply_local_edits(
        graph,
        [AddRoleAgentEdit(role_id="spec_auditor", after_node_id=builder)],
        role_pool=_pool(),
        check_invariants=False,
    )

    gate_report = [
        edge
        for edge in edited.edges
        if edge.destination_node == repairer and edge.destination_input == "gate_report"
    ]
    assert len(gate_report) == 1
    assert gate_report[0].condition is not None
    assert gate_report[0].condition.operator == "is_false"
    source = next(n for n in edited.nodes if n.node_id == gate_report[0].source_node)
    assert source.node_kind.value == "harness"


def test_a_condition_reading_a_field_its_source_cannot_emit_is_a_violation() -> None:
    """The silent-branch-disabling class, constructed directly.

    `EdgeCondition.evaluate` returns False for a missing field, so a condition
    re-sourced onto an agent's change artifact never fires and the branch simply
    does not run.
    """
    graph = _graph("gate_then_repair", harness_command=SPEC_HARNESS)
    author = _node_id(graph, "author")
    repairer = _node_id(graph, "gate_repairer")

    rewired = graph.model_copy(
        update={
            "edges": [
                edge.model_copy(
                    update={
                        "source_node": author,
                        "source_output": "repository_change",
                    }
                )
                if edge.destination_input == "gate_report"
                else edge
                for edge in graph.edges
            ]
        }
    )

    broken = [
        v for v in check_graph_invariants(rewired, pool=_pool())
        if v.invariant == "condition_field_available"
    ]
    assert len(broken) == 1
    assert "passed" in broken[0].message
    assert "RepositoryChangeArtifact" in broken[0].message
    assert repairer in {
        edge.destination_node
        for edge in rewired.edges
        if edge.edge_id == broken[0].edge_id
    }


def test_an_unconditional_gate_report_edge_is_a_violation() -> None:
    """A repair position that runs on the happy path is not a repair position."""
    graph = _graph("gate_then_repair", harness_command=SPEC_HARNESS)
    ungated = graph.model_copy(
        update={
            "edges": [
                edge.model_copy(update={"condition": None})
                if edge.destination_input == "gate_report"
                else edge
                for edge in graph.edges
            ]
        }
    )
    violations = check_graph_invariants(ungated, pool=_pool())
    assert any(v.invariant == "early_gate_has_consumer" for v in violations)


def test_a_change_edge_from_the_test_author_to_the_builder_bypasses_custody() -> None:
    """Restoring the author's change edge is how a suite leaks into the builder.

    Custody deletes the files; cutting this edge is what stops the patch quoting
    the suite into the next prompt. Both halves are the invariant.
    """
    graph = _graph("test_first", harness_command=SPEC_HARNESS)
    author = _node_id(graph, "test_author")
    builder = _node_id(graph, "builder")

    leaked = graph.model_copy(
        update={
            "edges": [
                *graph.edges,
                EdgeSpec(
                    edge_id="author_change_to_builder",
                    source_node=author,
                    source_output="repository_change",
                    destination_node=builder,
                    destination_input="upstream_change",
                ),
            ]
        }
    )
    violations = check_graph_invariants(leaked, pool=_pool())
    assert any(v.invariant == "custody_precedes_consumers" for v in violations)


def test_two_editing_agents_in_one_wave_is_a_violation() -> None:
    """The template loader refuses this shape; the edit layer does not.

    Two agents that both write, with no edge ordering them, share one working
    tree — so this is a corrupted workspace rather than a topology choice.
    """
    graph = _graph("solo", harness_command=SPEC_HARNESS)
    author = next(node for node in graph.nodes if node.node_id.endswith("_implementer"))
    twin = author.model_copy(update={"node_id": "agent_2_rival_implementer"})

    raced = graph.model_copy(update={"nodes": [*graph.nodes, twin]})
    violations = [
        v
        for v in check_graph_invariants(raced, pool=_pool())
        if v.invariant == "no_parallel_writers"
    ]
    assert len(violations) == 1
    assert author.node_id in violations[0].message
    assert twin.node_id in violations[0].message

    # Ordering them fixes it, and that is the point: the invariant is about the
    # wave, not about the count.
    serialised = raced.model_copy(
        update={
            "edges": [
                *raced.edges,
                EdgeSpec(
                    edge_id="author_to_rival",
                    source_node=author.node_id,
                    source_output="repository_change",
                    destination_node=twin.node_id,
                    destination_input="upstream_change",
                ),
            ]
        }
    )
    assert not [
        v
        for v in check_graph_invariants(serialised, pool=_pool())
        if v.invariant == "no_parallel_writers"
    ]


def test_a_privileged_tool_is_a_violation() -> None:
    graph = _graph("solo", harness_command=SPEC_HARNESS)
    author = _node_id(graph, "author")
    armed = graph.model_copy(
        update={
            "nodes": [
                node.model_copy(update={"tools": ["hidden_tests"]})
                if node.node_id == author
                else node
                for node in graph.nodes
            ]
        }
    )
    violations = check_graph_invariants(armed, pool=_pool())
    assert any(v.invariant == "no_privileged_tools" for v in violations)


def test_metadata_claiming_the_wrong_template_is_a_violation() -> None:
    """An edited graph keeps its old `template_id`, and readers believe it."""
    graph = _graph("solo", harness_command=SPEC_HARNESS)
    violations = check_graph_invariants(
        graph, pool=_pool(), expected_template_id="parallel_audit"
    )
    assert [v.invariant for v in violations] == ["template_id_describes_graph"]


def test_assert_raises_with_every_violation_named() -> None:
    graph = _graph("solo", harness_command=SPEC_HARNESS)
    with pytest.raises(GraphInvariantError) as excinfo:
        assert_graph_invariants(
            graph, pool=_pool(), expected_template_id="review_then_fix"
        )
    assert "template_id_describes_graph" in str(excinfo.value)


def test_an_unresolvable_artifact_type_is_left_alone() -> None:
    """Unknown is not wrong: a schema defined elsewhere must not fail every graph."""
    graph = _graph("gate_then_repair", harness_command=SPEC_HARNESS)
    exotic = graph.model_copy(
        update={
            "nodes": [
                node.model_copy(update={"output_slots": {"result": "SomeOtherArtifact"}})
                if node.node_kind.value == "harness"
                else node
                for node in graph.nodes
            ],
            "edges": [
                edge.model_copy(
                    update={
                        "condition": EdgeCondition(
                            source_field="not_a_field", operator="is_true"
                        )
                    }
                )
                if edge.condition is not None
                else edge
                for edge in graph.edges
            ],
        }
    )
    assert not [
        v
        for v in check_graph_invariants(exotic, pool=_pool())
        if v.invariant == "condition_field_available"
    ]


def test_dropping_the_last_editing_agent_is_still_refused_by_the_edit_engine() -> None:
    """The edit layer's own guards stay in force; this module adds to them."""
    from orchestra.control.fast_loop.schemas import DropAgentEdit

    graph = _graph("solo", harness_command=SPEC_HARNESS)
    author = _node_id(graph, "author")
    with pytest.raises(LocalEditError):
        apply_local_edits(
            graph, [DropAgentEdit(node_id=author)], role_pool=_pool()
        )
