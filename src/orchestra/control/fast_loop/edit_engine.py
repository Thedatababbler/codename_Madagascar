"""Immutable LocalEdit application on OrchestraGraph."""

from __future__ import annotations

from typing import Any

from orchestra.backends.base import ModelSpec
from orchestra.control.fast_loop.schemas import (
    AddRoleAgentEdit,
    AddVerifierNodeEdit,
    BudgetAdjustmentEdit,
    DropAgentEdit,
    LocalEdit,
    ModelOverrideEdit,
    PromptFeedbackEdit,
    RewireEdgeEdit,
    SessionPolicyEdit,
    ToolPolicyEdit,
)
from orchestra.ir.compiler import GraphCompiler
from orchestra.ir.edges import EdgeSpec
from orchestra.ir.graph import OrchestraGraph
from orchestra.ir.nodes import (
    AgentNodeSpec,
    HarnessNodeSpec,
    NodeKind,
    StructuredLLMBackendConfig,
)
from orchestra.roles.pool import RolePool, default_role_pool


class LocalEditError(ValueError):
    """Raised when a local edit is illegal or fails validation."""


# Tools that must never be auto-added by Fast Loop tool_policy edits.
_FORBIDDEN_AUTO_TOOLS = frozenset(
    {
        "shell_unrestricted",
        "filesystem_admin",
        "network_unrestricted",
        "private_evaluator",
        "hidden_tests",
    }
)


def apply_local_edits(
    base_graph: OrchestraGraph,
    edits: list[LocalEdit],
    *,
    compiler: GraphCompiler | None = None,
    max_timeout_seconds: float | None = None,
    max_steps_cap: int | None = None,
    role_pool: RolePool | None = None,
) -> OrchestraGraph:
    """Deep-copy base graph, apply edits, re-validate, and recompute hash lineage."""
    graph = base_graph.clone()
    parent_hash = base_graph.content_hash
    lineage: list[dict[str, Any]] = list(graph.metadata.get("edit_lineage") or [])

    for edit in edits:
        graph = _apply_one(
            graph,
            edit,
            max_timeout_seconds=max_timeout_seconds,
            max_steps_cap=max_steps_cap,
            role_pool=role_pool,
        )
        lineage.append(edit.model_dump(mode="json"))

    graph.metadata = {
        **dict(graph.metadata),
        "parent_graph_hash": parent_hash,
        "edit_lineage": lineage,
    }

    # Schema + DAG validation via compiler when provided.
    if compiler is not None:
        compiler.compile(graph)
    else:
        _validate_dag(graph)
    # Touch content_hash to ensure canonicalization succeeds.
    _ = graph.content_hash
    return graph


def _apply_one(
    graph: OrchestraGraph,
    edit: LocalEdit,
    *,
    max_timeout_seconds: float | None,
    max_steps_cap: int | None,
    role_pool: RolePool | None = None,
) -> OrchestraGraph:
    if isinstance(edit, PromptFeedbackEdit):
        return _replace_agent(
            graph,
            edit.node_id,
            lambda node: node.model_copy(
                update={
                    "prompt_feedback": _merge_feedback(node.prompt_feedback, edit.feedback)
                }
            ),
        )
    if isinstance(edit, ModelOverrideEdit):
        return _replace_agent(
            graph,
            edit.node_id,
            lambda node: node.model_copy(
                update={
                    "model": ModelSpec(
                        name=edit.model_name,
                        temperature=node.model.temperature if node.model else 0.2,
                        max_tokens=node.model.max_tokens if node.model else 4096,
                        provider=node.model.provider if node.model else "openai_compatible",
                    )
                }
            ),
        )
    if isinstance(edit, ToolPolicyEdit):
        forbidden = [t for t in edit.add_tools if t in _FORBIDDEN_AUTO_TOOLS]
        if forbidden:
            raise LocalEditError(
                f"tool_policy refuses to add dangerous tools: {forbidden}"
            )
        if any("private" in t.lower() or "hidden" in t.lower() for t in edit.add_tools):
            raise LocalEditError("tool_policy refuses private/hidden evaluator access")
        return _replace_agent(
            graph,
            edit.node_id,
            lambda node: node.model_copy(
                update={
                    "tools": sorted(
                        (set(node.tools) | set(edit.add_tools)) - set(edit.remove_tools)
                    )
                }
            ),
        )
    if isinstance(edit, BudgetAdjustmentEdit):
        return _replace_agent(
            graph,
            edit.node_id,
            lambda node: _apply_budget(
                node,
                edit,
                max_timeout_seconds=max_timeout_seconds,
                max_steps_cap=max_steps_cap,
            ),
        )
    if isinstance(edit, SessionPolicyEdit):
        return _replace_agent(
            graph,
            edit.node_id,
            lambda node: node.model_copy(update={"session_policy": edit.policy.value}),
        )
    if isinstance(edit, AddVerifierNodeEdit):
        return _add_verifier_node(graph, edit)
    if isinstance(edit, AddRoleAgentEdit):
        return _add_role_agent(graph, edit, role_pool=role_pool)
    if isinstance(edit, DropAgentEdit):
        return _drop_agent(graph, edit, role_pool=role_pool)
    if isinstance(edit, RewireEdgeEdit):
        return _rewire_edge(graph, edit)
    raise LocalEditError(f"unsupported edit type: {type(edit)!r}")


def _merge_feedback(existing: str | None, new: str) -> str:
    new = new.strip()
    if not existing:
        return new
    if new in existing:
        return existing
    return f"{existing.rstrip()}\n\n{new}"


def _apply_budget(
    node: AgentNodeSpec,
    edit: BudgetAdjustmentEdit,
    *,
    max_timeout_seconds: float | None,
    max_steps_cap: int | None,
) -> AgentNodeSpec:
    backend = node.resolved_backend()
    max_steps = getattr(backend, "max_steps", 1) + int(edit.max_steps_delta)
    if max_steps < 1:
        raise LocalEditError("budget_adjustment would reduce max_steps below 1")
    if max_steps_cap is not None and max_steps > max_steps_cap:
        raise LocalEditError(
            f"budget_adjustment max_steps={max_steps} exceeds cap={max_steps_cap}"
        )
    backend_data = backend.model_dump(mode="json")
    backend_data["max_steps"] = max_steps
    new_backend = type(backend).model_validate(backend_data)

    timeout = node.timeout_seconds
    if edit.timeout_seconds_delta:
        base = float(timeout or 60.0)
        timeout = base + float(edit.timeout_seconds_delta)
        if timeout <= 0:
            raise LocalEditError("budget_adjustment would make timeout non-positive")
        if max_timeout_seconds is not None and timeout > max_timeout_seconds:
            raise LocalEditError(
                f"budget_adjustment timeout={timeout} exceeds cap={max_timeout_seconds}"
            )
    return node.model_copy(update={"backend": new_backend, "timeout_seconds": timeout})


def _replace_agent(
    graph: OrchestraGraph,
    node_id: str,
    mutator,
) -> OrchestraGraph:
    nodes = []
    found = False
    for node in graph.nodes:
        if node.node_id != node_id:
            nodes.append(node)
            continue
        if node.node_kind is not NodeKind.AGENT:
            raise LocalEditError(f"node {node_id!r} is not an agent node")
        assert isinstance(node, AgentNodeSpec)
        nodes.append(mutator(node))
        found = True
    if not found:
        raise LocalEditError(f"unknown node_id: {node_id}")
    return graph.model_copy(update={"nodes": nodes})


def _add_verifier_node(
    graph: OrchestraGraph,
    edit: AddVerifierNodeEdit,
) -> OrchestraGraph:
    """Insert a lightweight structured_llm verifier after target agent when template allows."""
    target = next((n for n in graph.nodes if n.node_id == edit.target_node_id), None)
    if target is None:
        raise LocalEditError(f"unknown target_node_id: {edit.target_node_id}")
    if target.node_kind is not NodeKind.AGENT:
        raise LocalEditError("add_verifier_node requires an agent target")
    assert isinstance(target, AgentNodeSpec)

    if edit.verifier_template_id not in {"default_structured_verifier"}:
        raise LocalEditError(
            f"unknown verifier_template_id: {edit.verifier_template_id}"
        )

    verifier_id = f"{edit.target_node_id}__verifier"
    if any(n.node_id == verifier_id for n in graph.nodes):
        raise LocalEditError(f"verifier node already exists: {verifier_id}")

    # Only attach when an outgoing harness edge exists from target (repo edit path).
    harness_edges = [
        e
        for e in graph.edges
        if e.source_node == edit.target_node_id
        and any(
            n.node_id == e.destination_node and n.node_kind is NodeKind.HARNESS
            for n in graph.nodes
        )
    ]
    if not harness_edges:
        raise LocalEditError(
            "add_verifier_node requires an existing harness edge from the target"
        )

    out_slot, out_type = next(iter(target.output_slots.items()))
    verifier = AgentNodeSpec(
        node_id=verifier_id,
        contract_id=target.contract_id,
        backend=StructuredLLMBackendConfig(max_steps=1),
        model=target.model,
        output_contract=target.output_contract,
        input_slots={out_slot: out_type},
        output_slots={out_slot: out_type},
        timeout_seconds=min(float(target.timeout_seconds or 60.0), 60.0),
        prompt_feedback=(
            "Verify the previous repository change against the public tests. "
            "If incorrect, produce a corrected RepositoryChangeArtifact."
        ),
    )

    # Rewire: target → verifier → former destinations of target.
    new_edges: list[EdgeSpec] = []
    for edge in graph.edges:
        if edge.source_node == edit.target_node_id:
            new_edges.append(
                edge.model_copy(
                    update={
                        "edge_id": f"{edge.edge_id}__via_verifier",
                        "source_node": verifier_id,
                    }
                )
            )
        else:
            new_edges.append(edge)
    new_edges.append(
        EdgeSpec(
            edge_id=f"{edit.target_node_id}_to_{verifier_id}",
            source_node=edit.target_node_id,
            source_output=out_slot,
            destination_node=verifier_id,
            destination_input=out_slot,
        )
    )
    nodes = list(graph.nodes) + [verifier]
    updated = graph.model_copy(update={"nodes": nodes, "edges": new_edges})
    _validate_dag(updated)
    return updated


def _agent(graph: OrchestraGraph, node_id: str, *, what: str) -> AgentNodeSpec:
    node = next((n for n in graph.nodes if n.node_id == node_id), None)
    if node is None:
        raise LocalEditError(f"{what}: unknown node_id {node_id!r}")
    if node.node_kind is not NodeKind.AGENT:
        raise LocalEditError(f"{what}: {node_id!r} is not an agent node")
    assert isinstance(node, AgentNodeSpec)
    return node


def _editing_agents(graph: OrchestraGraph, pool: RolePool) -> list[AgentNodeSpec]:
    """Agents whose role writes to the repository.

    Read off the node id, which the subgraph builder composes as
    ``agent_<n>_<slot>_<role_id>``, because the node itself does not carry its
    role. A node whose role cannot be identified counts as editing: guessing
    "read-only" would let ``drop_agent`` delete the only agent doing the work.
    """
    editing: list[AgentNodeSpec] = []
    for node in graph.nodes:
        if node.node_kind is not NodeKind.AGENT:
            continue
        assert isinstance(node, AgentNodeSpec)
        role = role_of(node, pool)
        if role is None or role.edits_repository:
            editing.append(node)
    return editing


def role_of(node: AgentNodeSpec, pool: RolePool):  # noqa: ANN202 - RoleSpec | None
    return pool.role_for_node_id(node.node_id)


def _add_role_agent(
    graph: OrchestraGraph,
    edit: AddRoleAgentEdit,
    *,
    role_pool: RolePool | None,
) -> OrchestraGraph:
    """Insert a role-pool agent in series after ``after_node_id``.

    The new node reuses its anchor's contract: the compiler rejects a
    ``contract_id`` that is not in the registry, and the registry is fixed for
    the duration of a run, so there is no way to introduce a genuinely new
    contract mid-flight. What the role contributes is its prompt and its
    budgets, which is the part the search is varying.
    """
    pool = role_pool or default_role_pool()
    role = pool.get(edit.role_id)
    if role is None:
        raise LocalEditError(f"add_role_agent: unknown role_id {edit.role_id!r}")
    if edit.parallel and role.edits_repository:
        raise LocalEditError(
            "add_role_agent refuses parallel placement for a repository-editing "
            "role: concurrent writers would share one workspace"
        )
    if edit.parallel:
        raise LocalEditError("add_role_agent supports serial placement only")

    anchor = _agent(graph, edit.after_node_id, what="add_role_agent")
    node_id = f"{edit.after_node_id}__{edit.role_id}"[:64]
    if any(n.node_id == node_id for n in graph.nodes):
        raise LocalEditError(f"add_role_agent: node already exists: {node_id}")
    outgoing = [e for e in graph.edges if e.source_node == edit.after_node_id]
    if not outgoing:
        raise LocalEditError(
            "add_role_agent needs an outgoing edge from the anchor to rewire"
        )

    out_slot, out_type = next(iter(anchor.output_slots.items()))
    backend = anchor.resolved_backend()
    backend_data = backend.model_dump(mode="json")
    backend_data["max_steps"] = role.max_steps
    if not role.edits_repository and "require_git_diff" in backend_data:
        # A reviewer that reports instead of editing produces no diff, and a
        # backend demanding one would score its correct behaviour a failure.
        backend_data["require_git_diff"] = False
    prelude = "\n\n".join(
        part for part in (str(anchor.prompt_prelude or "").strip(), role.prompt.strip()) if part
    )
    # The shared contract declares an input schema, and the compiler requires the
    # node to declare a slot carrying it -- so mirroring only the anchor's output
    # produces a graph that fails to compile. Carry the anchor's run-level inputs
    # (the ones fed from the initial bundle, so they resolve without an inbound
    # edge) and add the anchor's output as the upstream slot.
    input_slots = {
        name: schema
        for name, schema in anchor.input_slots.items()
        if name in graph.initial_artifact_slots
    }
    input_slots[out_slot] = out_type
    inserted = AgentNodeSpec(
        node_id=node_id,
        contract_id=anchor.contract_id,
        backend=type(backend).model_validate(backend_data),
        model=(
            anchor.model.model_copy(update={"max_tokens": role.max_tokens})
            if anchor.model
            else None
        ),
        output_contract=anchor.output_contract,
        input_slots=input_slots,
        output_slots={out_slot: out_type},
        timeout_seconds=role.timeout_seconds,
        prompt_prelude=prelude or None,
    )

    edges = [
        e.model_copy(
            update={"edge_id": f"{e.edge_id}__via_{edit.role_id}"[:96], "source_node": node_id}
        )
        if e.source_node == edit.after_node_id
        else e
        for e in graph.edges
    ]
    edges.append(
        EdgeSpec(
            edge_id=f"{edit.after_node_id}_to_{node_id}"[:96],
            source_node=edit.after_node_id,
            source_output=out_slot,
            destination_node=node_id,
            destination_input=out_slot,
        )
    )
    updated = graph.model_copy(update={"nodes": [*graph.nodes, inserted], "edges": edges})
    _validate_dag(updated)
    return updated


def _drop_agent(
    graph: OrchestraGraph,
    edit: DropAgentEdit,
    *,
    role_pool: RolePool | None,
) -> OrchestraGraph:
    """Remove a non-editing agent and reconnect around it."""
    pool = role_pool or default_role_pool()
    target = _agent(graph, edit.node_id, what="drop_agent")
    role = role_of(target, pool)
    if role is None or role.edits_repository:
        raise LocalEditError(
            f"drop_agent refuses {edit.node_id!r}: only a role known to be "
            "read-only may be removed"
        )
    remaining_editors = [n for n in _editing_agents(graph, pool) if n.node_id != edit.node_id]
    if not remaining_editors:
        raise LocalEditError("drop_agent would leave the milestone with no editing agent")

    incoming = [e for e in graph.edges if e.destination_node == edit.node_id]
    outgoing = [e for e in graph.edges if e.source_node == edit.node_id]
    kept = [
        e
        for e in graph.edges
        if e.source_node != edit.node_id and e.destination_node != edit.node_id
    ]
    # Reconnect every upstream to every downstream so a dropped middle node does
    # not sever the chain. Conditional inbound edges are not carried across: the
    # condition guarded entry to the node being removed, not to its successors.
    bridged: list[EdgeSpec] = []
    for up in incoming:
        if up.condition is not None:
            continue
        for down in outgoing:
            bridged.append(
                EdgeSpec(
                    edge_id=f"{up.source_node}_to_{down.destination_node}__bridged"[:96],
                    source_node=up.source_node,
                    source_output=up.source_output,
                    destination_node=down.destination_node,
                    destination_input=down.destination_input,
                    condition=down.condition,
                )
            )
    seen: set[str] = set()
    edges: list[EdgeSpec] = []
    for edge in [*kept, *bridged]:
        if edge.edge_id in seen:
            continue
        seen.add(edge.edge_id)
        edges.append(edge)
    nodes = [n for n in graph.nodes if n.node_id != edit.node_id]
    updated = graph.model_copy(update={"nodes": nodes, "edges": edges})
    _validate_dag(updated)
    _require_reachable_terminals(updated)
    return updated


def _rewire_edge(graph: OrchestraGraph, edit: RewireEdgeEdit) -> OrchestraGraph:
    """Gate an edge on an upstream field, or make it unconditional."""
    target = next((e for e in graph.edges if e.edge_id == edit.edge_id), None)
    if target is None:
        raise LocalEditError(f"rewire_edge: unknown edge_id {edit.edge_id!r}")
    if edit.clear_condition and target.condition is None:
        raise LocalEditError(f"rewire_edge: edge {edit.edge_id!r} has no condition to clear")
    destination = next(
        (n for n in graph.nodes if n.node_id == target.destination_node), None
    )
    if (
        edit.clear_condition
        and destination is not None
        and destination.node_kind is NodeKind.TRANSFORM
    ):
        # The condition into the freeze transform *is* the acceptance gate.
        # Clearing it commits work the harness rejected, which would show up as
        # a candidate that scored well rather than as an illegal edit.
        raise LocalEditError(
            "rewire_edge refuses to clear the condition gating a transform: "
            f"{edit.edge_id!r} guards the commit"
        )
    condition = None if edit.clear_condition else edit.condition
    edges = [
        e.model_copy(update={"condition": condition}) if e.edge_id == edit.edge_id else e
        for e in graph.edges
    ]
    updated = graph.model_copy(update={"edges": edges})
    _validate_dag(updated)
    _require_reachable_terminals(updated)
    return updated


def _require_reachable_terminals(graph: OrchestraGraph) -> None:
    """Every harness and transform node must still have a live inbound path.

    An edit that orphans the freeze or the acceptance gate produces a graph that
    compiles and then commits nothing, which is worse than a rejected edit
    because it reads as a run that simply scored badly.
    """
    for node in graph.nodes:
        if node.node_kind is NodeKind.AGENT:
            continue
        if not any(e.destination_node == node.node_id for e in graph.edges):
            raise LocalEditError(
                f"edit orphaned {node.node_kind.value} node {node.node_id!r}"
            )


def _validate_dag(graph: OrchestraGraph) -> None:
    ids = {n.node_id for n in graph.nodes}
    for edge in graph.edges:
        if edge.source_node not in ids or edge.destination_node not in ids:
            raise LocalEditError(
                f"illegal edge {edge.edge_id}: unknown endpoint "
                f"{edge.source_node!r} -> {edge.destination_node!r}"
            )
    # Cycle check (Kahn).
    indeg = {nid: 0 for nid in ids}
    succ: dict[str, list[str]] = {nid: [] for nid in ids}
    for edge in graph.edges:
        succ[edge.source_node].append(edge.destination_node)
        indeg[edge.destination_node] += 1
    queue = [nid for nid, d in indeg.items() if d == 0]
    seen = 0
    while queue:
        nid = queue.pop()
        seen += 1
        for nxt in succ[nid]:
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                queue.append(nxt)
    if seen != len(ids):
        raise LocalEditError("local edit produced a cyclic graph")
    # Ensure harness nodes stay public visibility.
    for node in graph.nodes:
        if isinstance(node, HarnessNodeSpec) and node.visibility != "public":
            raise LocalEditError("private/hidden harness nodes are forbidden")
