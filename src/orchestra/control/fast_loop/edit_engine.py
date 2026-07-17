"""Immutable LocalEdit application on OrchestraGraph."""

from __future__ import annotations

from typing import Any

from orchestra.backends.base import ModelSpec
from orchestra.control.fast_loop.schemas import (
    AddVerifierNodeEdit,
    BudgetAdjustmentEdit,
    LocalEdit,
    ModelOverrideEdit,
    PromptFeedbackEdit,
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
