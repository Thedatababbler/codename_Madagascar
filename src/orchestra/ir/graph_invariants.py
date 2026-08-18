"""Structural invariants a milestone subgraph must satisfy, in any layer.

The compiler checks that a graph is well-formed: schemas match, slots resolve,
the DAG is acyclic. None of that catches a graph that is well-formed and
*meaningless* — a gate scoring a reviewer's empty diff, a repair branch behind a
condition nobody can satisfy, a suite the implementer can read. Those are the
failures worth a validator, because they do not raise: the candidate runs, a
number comes back, and the frontier records it as though a design had been
measured.

The invariants live here, above both producers, because there are two:

* the plan layer compiles a template into a graph (``build_milestone_graph``),
  where the template's own rules apply at load time; and
* the fast loop mutates a compiled graph (``apply_local_edits``), where they do
  not.

See ``docs/topology_and_edit.md`` for the layer split and for why each invariant
is on this list.

Violations are returned rather than raised, so a caller can report all of them at
once, and are named with stable ids so a rejection message can be grepped.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from orchestra.ir.graph import OrchestraGraph
from orchestra.ir.nodes import AgentNodeSpec, NodeKind, NodeSpec
from orchestra.roles.pool import RolePool, default_role_pool

#: Tools no automatically generated design may hold, whatever produced it.
FORBIDDEN_TOOLS = frozenset(
    {
        "filesystem_admin",
        "network_unrestricted",
        "private_evaluator",
        "hidden_tests",
    }
)

#: The slot every repository-editing agent publishes its work on.
CHANGE_SLOT = "repository_change"

#: The input a milestone's conditional repair position waits on.
GATE_REPORT_SLOT = "gate_report"

#: The input a consumer of an authored suite waits on, so that it cannot start
#: until custody has moved the suite out of the workspace.
CUSTODY_SLOT = "suite_custody"


@dataclass(frozen=True)
class InvariantViolation:
    """One broken invariant, identified well enough to act on."""

    invariant: str
    message: str
    node_id: str | None = None
    edge_id: str | None = None

    def __str__(self) -> str:
        where = self.node_id or self.edge_id
        return f"{self.invariant}: {self.message}" + (f" [{where}]" if where else "")


class GraphInvariantError(ValueError):
    """Raised by ``assert_graph_invariants`` when any invariant is broken."""

    def __init__(self, violations: Sequence[InvariantViolation]) -> None:
        self.violations = list(violations)
        super().__init__("; ".join(str(v) for v in self.violations))


def check_graph_invariants(
    graph: OrchestraGraph,
    *,
    pool: RolePool | None = None,
    expected_template_id: str | None = None,
) -> list[InvariantViolation]:
    """Every invariant this graph breaks.

    ``expected_template_id`` is checked only when supplied: the edit layer has no
    template to compare against, and demanding one there would reject every
    parameter edit.
    """
    role_pool = pool or default_role_pool()
    nodes = {node.node_id: node for node in graph.nodes}
    violations: list[InvariantViolation] = []
    violations += _check_gate_scores_editing_agent(graph, nodes, role_pool)
    violations += _check_final_output_reachable(graph, nodes)
    violations += _check_early_gate_has_consumer(graph, nodes)
    violations += _check_custody_precedes_consumers(graph, nodes, role_pool)
    violations += _check_no_parallel_writers(graph, nodes, role_pool)
    violations += _check_condition_fields_available(graph, nodes)
    violations += _check_read_only_agents_emit_no_diff(graph, role_pool)
    violations += _check_harness_and_tools(graph)
    violations += _check_template_id(graph, expected_template_id)
    return violations


def assert_graph_invariants(
    graph: OrchestraGraph,
    *,
    pool: RolePool | None = None,
    expected_template_id: str | None = None,
) -> None:
    violations = check_graph_invariants(
        graph, pool=pool, expected_template_id=expected_template_id
    )
    if violations:
        raise GraphInvariantError(violations)


# --- 1. the gate scores an editing agent ------------------------------------


def _check_gate_scores_editing_agent(
    graph: OrchestraGraph,
    nodes: Mapping[str, NodeSpec],
    pool: RolePool,
) -> list[InvariantViolation]:
    """A harness must grade work, not a report.

    Every read-only agent still declares a ``repository_change`` output, because
    the runtime's contract is uniform, so a reviewer wired into a gate produces a
    perfectly valid empty diff and the gate scores *that*. The score comes back
    plausible and describes nothing.
    """
    out: list[InvariantViolation] = []
    for edge in graph.edges:
        destination = nodes.get(edge.destination_node)
        if destination is None or destination.node_kind is not NodeKind.HARNESS:
            continue
        if edge.destination_input != CHANGE_SLOT:
            continue
        source = nodes.get(edge.source_node)
        if source is None or source.node_kind is not NodeKind.AGENT:
            continue
        role = pool.role_for_node_id(source.node_id)
        if role is not None and not role.edits_repository:
            out.append(
                InvariantViolation(
                    invariant="gate_scores_editing_agent",
                    message=(
                        f"harness {destination.node_id!r} grades {source.node_id!r}, "
                        f"whose role {role.role_id!r} never edits the repository, so "
                        "the graded artifact is an empty diff"
                    ),
                    edge_id=edge.edge_id,
                )
            )
    return out


# --- 2. the final output stays reachable ------------------------------------


def _check_final_output_reachable(
    graph: OrchestraGraph, nodes: Mapping[str, NodeSpec]
) -> list[InvariantViolation]:
    slot = graph.final_output_slot
    if not slot:
        return []
    producers = [node for node in graph.nodes if slot in node.output_slots]
    if not producers:
        return [
            InvariantViolation(
                invariant="final_output_reachable",
                message=f"no node produces the final output slot {slot!r}",
            )
        ]
    out: list[InvariantViolation] = []
    for node in producers:
        if node.node_kind is NodeKind.AGENT:
            continue
        if not any(edge.destination_node == node.node_id for edge in graph.edges):
            out.append(
                InvariantViolation(
                    invariant="final_output_reachable",
                    message=(
                        f"{node.node_id!r} produces {slot!r} but nothing feeds it, so "
                        "the milestone would commit nothing"
                    ),
                    node_id=node.node_id,
                )
            )
    return out


# --- 3. an early gate has something waiting on failure ----------------------


def _check_early_gate_has_consumer(
    graph: OrchestraGraph, nodes: Mapping[str, NodeSpec]
) -> list[InvariantViolation]:
    """An early probe is only worth its cost if a slot waits behind a failure.

    Both halves are checked. A probe with nothing behind it is a harness run
    nobody reads; a node holding a ``gate_report`` input over an unconditional
    edge is a repair position that runs on the happy path too, which is the cost
    the conditional edge existed to avoid.
    """
    out: list[InvariantViolation] = []
    graded = _harness_nodes_grading_change(graph, nodes)
    failure_gated = [
        edge
        for edge in graph.edges
        if edge.condition is not None and edge.condition.operator == "is_false"
    ]
    if len(graded) > 1 and not failure_gated:
        out.append(
            InvariantViolation(
                invariant="early_gate_has_consumer",
                message=(
                    f"{len(graded)} harnesses grade a change but no edge is gated on "
                    "failure, so the earlier one is paid for and never read"
                ),
            )
        )
    for edge in graph.edges:
        if edge.destination_input != GATE_REPORT_SLOT:
            continue
        if edge.condition is None:
            out.append(
                InvariantViolation(
                    invariant="early_gate_has_consumer",
                    message=(
                        f"{edge.destination_node!r} receives a gate report over an "
                        "unconditional edge, so it runs whether or not the gate failed"
                    ),
                    edge_id=edge.edge_id,
                )
            )
    return out


# --- 4. custody comes between the author and every consumer -----------------


def _check_custody_precedes_consumers(
    graph: OrchestraGraph,
    nodes: Mapping[str, NodeSpec],
    pool: RolePool,
) -> list[InvariantViolation]:
    """The implementer must not be able to read the authored suite.

    Custody deletes the suite from the workspace, and cutting the author's change
    edge is what stops the patch quoting the whole suite into the next prompt.
    Either half restored puts the milestone back to scoring compliance with a
    visible checklist (EXP-20260811-01).
    """
    if not _has_custody_step(graph, nodes):
        return []
    authors = [
        node.node_id
        for node in graph.nodes
        if node.node_kind is NodeKind.AGENT
        and _role_id(pool, node.node_id) == "test_author"
    ]
    out: list[InvariantViolation] = []
    for edge in graph.edges:
        if edge.source_node not in authors:
            continue
        destination = nodes.get(edge.destination_node)
        if destination is None or destination.node_kind is not NodeKind.AGENT:
            continue
        out.append(
            InvariantViolation(
                invariant="custody_precedes_consumers",
                message=(
                    f"{edge.destination_node!r} reads the authored suite's change "
                    f"artifact directly from {edge.source_node!r}, bypassing custody"
                ),
                edge_id=edge.edge_id,
            )
        )
    return out


def _has_custody_step(
    graph: OrchestraGraph, nodes: Mapping[str, NodeSpec]
) -> bool:
    """Whether some harness feeds a ``suite_custody`` input.

    Recognised by what it feeds rather than by node id, so a renamed custody step
    is still recognised and a graph without one is not checked against a rule that
    does not apply to it.
    """
    for edge in graph.edges:
        if edge.destination_input != CUSTODY_SLOT:
            continue
        source = nodes.get(edge.source_node)
        if source is not None and source.node_kind is NodeKind.HARNESS:
            return True
    return False


def _role_id(pool: RolePool, node_id: str) -> str:
    role = pool.role_for_node_id(node_id)
    return role.role_id if role is not None else ""


# --- 5. two repository writers never share a wave ---------------------------


def _check_no_parallel_writers(
    graph: OrchestraGraph,
    nodes: Mapping[str, NodeSpec],
    pool: RolePool,
) -> list[InvariantViolation]:
    """Concurrent writers share one working tree, so this is corruption.

    An agent whose role cannot be identified counts as a writer: assuming
    read-only would let exactly the corruption this check exists for through.
    """
    depth = _depths(graph)
    writers_by_depth: dict[int, list[str]] = {}
    for node in graph.nodes:
        if node.node_kind is not NodeKind.AGENT:
            continue
        role = pool.role_for_node_id(node.node_id)
        if role is not None and not role.edits_repository:
            continue
        writers_by_depth.setdefault(depth.get(node.node_id, 0), []).append(node.node_id)
    out: list[InvariantViolation] = []
    for level, writers in sorted(writers_by_depth.items()):
        if len(writers) < 2:
            continue
        out.append(
            InvariantViolation(
                invariant="no_parallel_writers",
                message=(
                    f"{sorted(writers)} run in the same wave (depth {level}) and may "
                    "all edit the shared workspace"
                ),
            )
        )
    return out


def _depths(graph: OrchestraGraph) -> dict[str, int]:
    """Longest-path depth per node, which is the wave it runs in."""
    depth = {node.node_id: 0 for node in graph.nodes}
    for _ in range(len(graph.nodes)):
        changed = False
        for edge in graph.edges:
            if edge.source_node not in depth or edge.destination_node not in depth:
                continue
            if depth[edge.destination_node] < depth[edge.source_node] + 1:
                depth[edge.destination_node] = depth[edge.source_node] + 1
                changed = True
        if not changed:
            break
    return depth


# --- 6. a condition reads a field its source emits --------------------------


def _check_condition_fields_available(
    graph: OrchestraGraph, nodes: Mapping[str, NodeSpec]
) -> list[InvariantViolation]:
    """A mis-sourced condition disables a branch silently.

    ``EdgeCondition.evaluate`` walks the payload and returns ``False`` for a field
    it cannot find, so an edit that re-sources a conditional edge to a node
    emitting a different artifact turns the branch off rather than erroring. That
    is the shape of the ``add_role_agent`` defect this module was written for.

    An artifact type this process cannot resolve is left alone. Unknown is not the
    same as wrong, and rejecting on it would fail every graph carrying a schema
    defined outside ``orchestra.schemas.artifacts``.
    """
    out: list[InvariantViolation] = []
    for edge in graph.edges:
        if edge.condition is None:
            continue
        source = nodes.get(edge.source_node)
        if source is None:
            continue
        artifact_type = source.output_slots.get(edge.source_output)
        fields = _artifact_fields(artifact_type)
        if fields is None:
            continue
        root = edge.condition.source_field.split(".")[0]
        if root not in fields:
            out.append(
                InvariantViolation(
                    invariant="condition_field_available",
                    message=(
                        f"condition reads {edge.condition.source_field!r} but "
                        f"{edge.source_node!r} emits {artifact_type} on "
                        f"{edge.source_output!r}, which has no such field — the branch "
                        "would be silently disabled"
                    ),
                    edge_id=edge.edge_id,
                )
            )
    return out


def _artifact_fields(artifact_type: str | None) -> frozenset[str] | None:
    if not artifact_type:
        return None
    from orchestra.schemas import artifacts as artifact_module

    model = getattr(artifact_module, artifact_type, None)
    fields = getattr(model, "model_fields", None)
    if not isinstance(fields, dict):
        return None
    return frozenset(fields)


# --- 7. read-only agents are not asked for a diff ---------------------------


def _check_read_only_agents_emit_no_diff(
    graph: OrchestraGraph, pool: RolePool
) -> list[InvariantViolation]:
    """A backend demanding a diff scores a reviewer's correct behaviour a failure."""
    out: list[InvariantViolation] = []
    for node in graph.nodes:
        if node.node_kind is not NodeKind.AGENT:
            continue
        assert isinstance(node, AgentNodeSpec)
        role = pool.role_for_node_id(node.node_id)
        if role is None or role.edits_repository:
            continue
        backend = node.resolved_backend()
        if getattr(backend, "require_git_diff", False):
            out.append(
                InvariantViolation(
                    invariant="read_only_agents_emit_no_diff",
                    message=(
                        f"{node.node_id!r} holds read-only role {role.role_id!r} but its "
                        "backend requires a git diff"
                    ),
                    node_id=node.node_id,
                )
            )
    return out


# --- 8. harnesses stay public, tools stay tame ------------------------------


def _check_harness_and_tools(graph: OrchestraGraph) -> list[InvariantViolation]:
    out: list[InvariantViolation] = []
    for node in graph.nodes:
        if node.node_kind is NodeKind.HARNESS:
            if getattr(node, "visibility", "public") != "public":
                out.append(
                    InvariantViolation(
                        invariant="harness_public_only",
                        message=f"{node.node_id!r} is not a public harness",
                        node_id=node.node_id,
                    )
                )
            continue
        if node.node_kind is not NodeKind.AGENT:
            continue
        assert isinstance(node, AgentNodeSpec)
        forbidden = sorted(set(node.tools) & FORBIDDEN_TOOLS)
        if forbidden:
            out.append(
                InvariantViolation(
                    invariant="no_privileged_tools",
                    message=f"{node.node_id!r} holds forbidden tools {forbidden}",
                    node_id=node.node_id,
                )
            )
    return out


# --- 9. the recorded template describes the graph ---------------------------


def _check_template_id(
    graph: OrchestraGraph, expected_template_id: str | None
) -> list[InvariantViolation]:
    """Metadata that claims a template it no longer matches misleads readers.

    Absent metadata is fine — plenty of graphs predate templates. A *wrong* value
    is not, because everything downstream believes it.
    """
    if expected_template_id is None:
        return []
    recorded = str((graph.metadata or {}).get("template_id") or "")
    if recorded and recorded != expected_template_id:
        return [
            InvariantViolation(
                invariant="template_id_describes_graph",
                message=(
                    f"metadata says template {recorded!r} but this graph was built as "
                    f"{expected_template_id!r}"
                ),
            )
        ]
    return []


def _harness_nodes_grading_change(
    graph: OrchestraGraph, nodes: Mapping[str, NodeSpec]
) -> list[str]:
    graded: list[str] = []
    for edge in graph.edges:
        destination = nodes.get(edge.destination_node)
        if destination is None or destination.node_kind is not NodeKind.HARNESS:
            continue
        if edge.destination_input == CHANGE_SLOT and destination.node_id not in graded:
            graded.append(destination.node_id)
    return graded


def violations_for(
    graph: OrchestraGraph,
    invariants: Iterable[str],
    *,
    pool: RolePool | None = None,
) -> list[InvariantViolation]:
    """Only the named invariants, for callers enforcing a subset."""
    wanted = set(invariants)
    return [v for v in check_graph_invariants(graph, pool=pool) if v.invariant in wanted]


__all__ = [
    "FORBIDDEN_TOOLS",
    "GraphInvariantError",
    "InvariantViolation",
    "assert_graph_invariants",
    "check_graph_invariants",
    "violations_for",
]
