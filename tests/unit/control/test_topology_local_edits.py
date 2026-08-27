"""Atomic topology edits, which are what make the fast loop a design search.

The graph built here is the shape the subgraph builder actually emits for a
``review_then_fix`` milestone (author -> reviewer -> fixer, then the acceptance
harness and a freeze transform gated on it), because every one of these edits is
about rewiring precisely that: an edit that only holds on a two-node toy would
tell us nothing about the graphs the search will run on.
"""

from __future__ import annotations

import pytest
from milestone_subgraph import AUTHOR, FIXER, REVIEWER, review_then_fix_graph

from orchestra.control.fast_loop.edit_engine import LocalEditError, apply_local_edits
from orchestra.control.fast_loop.schemas import (
    AddRoleAgentEdit,
    DropAgentEdit,
    RewireEdgeEdit,
)
from orchestra.ir.edges import EdgeCondition
from orchestra.ir.graph import OrchestraGraph


def _graph() -> OrchestraGraph:
    return review_then_fix_graph()


def _sources_of(graph: OrchestraGraph, edge_id_prefix: str) -> set[str]:
    return {e.source_node for e in graph.edges if e.edge_id.startswith(edge_id_prefix)}


class TestAddRoleAgent:
    def test_the_inserted_agent_takes_over_the_anchors_outgoing_edges(self) -> None:
        base = _graph()
        edited = apply_local_edits(
            base, [AddRoleAgentEdit(role_id="edge_case_hardener", after_node_id=FIXER)]
        )
        inserted = f"{FIXER}__edge_case_hardener"[:64]
        assert any(n.node_id == inserted for n in edited.nodes)
        # The harness and the freeze must now read the inserted agent's output,
        # or the milestone would be graded on work done before the insertion.
        assert _sources_of(edited, "agent_to_tests") == {inserted}
        assert _sources_of(edited, "agent_to_freeze") == {inserted}
        assert any(
            e.source_node == FIXER and e.destination_node == inserted for e in edited.edges
        )

    def test_the_role_prompt_is_carried_without_erasing_the_runners_prelude(self) -> None:
        base = _graph()
        with_prelude = base.model_copy(
            update={
                "nodes": [
                    n.model_copy(update={"prompt_prelude": "WORKSPACE CONTEXT"})
                    if n.node_id == FIXER
                    else n
                    for n in base.nodes
                ]
            }
        )
        edited = apply_local_edits(
            with_prelude,
            [AddRoleAgentEdit(role_id="edge_case_hardener", after_node_id=FIXER)],
        )
        inserted = next(
            n for n in edited.nodes if n.node_id.endswith("__edge_case_hardener")
        )
        assert inserted.prompt_prelude is not None
        assert "WORKSPACE CONTEXT" in inserted.prompt_prelude
        assert len(inserted.prompt_prelude) > len("WORKSPACE CONTEXT")

    def test_a_read_only_role_is_not_asked_for_a_diff_it_will_not_produce(self) -> None:
        # Anchored on the author rather than the fixer: the fixer feeds the gate,
        # and inserting a read-only role there hands the gate an empty diff to
        # grade, which the invariant check now refuses outright.
        edited = apply_local_edits(
            _graph(), [AddRoleAgentEdit(role_id="spec_auditor", after_node_id=AUTHOR)]
        )
        inserted = next(n for n in edited.nodes if n.node_id.endswith("__spec_auditor"))
        assert inserted.resolved_backend().require_git_diff is False

    def test_splicing_a_read_only_role_in_front_of_the_gate_is_refused(self) -> None:
        """The edit is legal on its own terms and produces a meaningless score.

        `_add_role_agent` re-sources every outgoing edge of its anchor, so the
        acceptance harness ends up grading the inserted node. A reviewer's diff is
        empty, so the milestone would be scored on a change nobody made.
        """
        with pytest.raises(LocalEditError, match="empty diff"):
            apply_local_edits(
                _graph(),
                [AddRoleAgentEdit(role_id="spec_auditor", after_node_id=FIXER)],
            )

    def test_parallel_placement_of_an_editing_role_is_refused(self) -> None:
        with pytest.raises(LocalEditError, match="share one workspace"):
            apply_local_edits(
                _graph(),
                [
                    AddRoleAgentEdit(
                        role_id="implementer", after_node_id=FIXER, parallel=True
                    )
                ],
            )

    def test_an_unknown_role_is_refused(self) -> None:
        with pytest.raises(LocalEditError, match="unknown role_id"):
            apply_local_edits(
                _graph(), [AddRoleAgentEdit(role_id="not_a_role", after_node_id=FIXER)]
            )


class TestDropAgent:
    def test_dropping_a_middle_reviewer_bridges_its_neighbours(self) -> None:
        edited = apply_local_edits(_graph(), [DropAgentEdit(node_id=REVIEWER)])
        assert not any(n.node_id == REVIEWER for n in edited.nodes)
        # Without the bridge the fixer loses its only upstream input and the
        # chain silently shortens to one agent.
        assert any(
            e.source_node == AUTHOR
            and e.destination_node == FIXER
            and e.destination_input == "upstream_change"
            for e in edited.edges
        )

    def test_an_editing_agent_is_never_dropped(self) -> None:
        with pytest.raises(LocalEditError, match="read-only"):
            apply_local_edits(_graph(), [DropAgentEdit(node_id=FIXER)])

    def test_dropping_the_last_read_only_agent_leaves_the_gate_reachable(self) -> None:
        edited = apply_local_edits(_graph(), [DropAgentEdit(node_id=REVIEWER)])
        for node_id in ("repository_tests", "freeze_change"):
            assert any(e.destination_node == node_id for e in edited.edges)


class TestRewireEdge:
    def test_gating_an_edge_on_a_field_its_source_emits(self) -> None:
        edited = apply_local_edits(
            _graph(),
            [
                RewireEdgeEdit(
                    edge_id="link_reviewer_to_fixer",
                    condition=EdgeCondition(
                        source_field="changed_files", operator="not_equals", value=[]
                    ),
                )
            ],
        )
        edge = next(e for e in edited.edges if e.edge_id == "link_reviewer_to_fixer")
        assert edge.condition is not None
        assert edge.condition.source_field == "changed_files"

    def test_gating_an_agent_edge_on_a_harness_field_is_refused(self) -> None:
        """`passed` reaches an edge only from a harness, and this edge has an agent.

        `EdgeCondition.evaluate` returns False for a field it cannot find, so this
        gate would never open and the fixer would simply never run — a design the
        frontier would score as though it had been evaluated.
        """
        with pytest.raises(LocalEditError, match="silently disabled"):
            apply_local_edits(
                _graph(),
                [
                    RewireEdgeEdit(
                        edge_id="link_reviewer_to_fixer",
                        condition=EdgeCondition(
                            source_field="passed", operator="is_false"
                        ),
                    )
                ],
            )

    def test_the_condition_guarding_the_commit_cannot_be_cleared(self) -> None:
        with pytest.raises(LocalEditError, match="guards the commit"):
            apply_local_edits(
                _graph(),
                [RewireEdgeEdit(edge_id="tests_pass_to_freeze", clear_condition=True)],
            )

    def test_an_unknown_edge_is_refused(self) -> None:
        with pytest.raises(LocalEditError, match="unknown edge_id"):
            apply_local_edits(
                _graph(),
                [
                    RewireEdgeEdit(
                        edge_id="nope",
                        condition=EdgeCondition(source_field="passed", operator="is_true"),
                    )
                ],
            )


def test_every_topology_edit_leaves_the_base_graph_untouched() -> None:
    base = _graph()
    before = base.content_hash
    for edit in (
        AddRoleAgentEdit(role_id="edge_case_hardener", after_node_id=FIXER),
        DropAgentEdit(node_id=REVIEWER),
        RewireEdgeEdit(
            edge_id="link_reviewer_to_fixer",
            condition=EdgeCondition(
                source_field="changed_files", operator="not_equals", value=[]
            ),
        ),
    ):
        edited = apply_local_edits(base, [edit])
        assert base.content_hash == before
        assert edited.content_hash != before
        assert edited.metadata["parent_graph_hash"] == before
