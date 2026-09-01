"""Candidates that recompile a milestone instead of mutating its graph.

The edit layer changes an already-compiled graph in place, so the designs it can
reach are the ones a splice or a rewire produces. A milestone whose problem is
its *shape* is not among them. Turning a one-agent milestone into
implement-gate-repair means adding a harness, a conditional edge and a slot that
only becomes runnable behind a failure; the edits that try instead re-source the
gate onto a reviewer, and the invariant check now rejects the result — correctly,
because it grades an empty diff.

Going back to the plan is the alternative, and it is cheaper than a taxonomy of
special-cased edits. This module reads the draft a milestone was compiled from,
refills the slots of a different template, and compiles that through the same
builder. What comes out satisfies the same invariants by construction rather than
by repair.

The recompiled milestone keeps its objective, its acceptance checks and its
harness command, so a candidate is graded by the same yardstick as the attempt it
is compared against — including the authored suite frozen for this milestone,
which that command names.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

from orchestra.control.fast_loop.schemas import LocalCandidate, PlanRecompile
from orchestra.ir.compiler import GraphCompiler
from orchestra.ir.contracts import AgentContract, load_contracts
from orchestra.ir.graph import OrchestraGraph, load_graph
from orchestra.ir.nodes import NodeKind
from orchestra.realbench.milestone_planner import (
    AgentDraft,
    MilestoneAcceptance,
    MilestoneDraft,
)
from orchestra.realbench.subgraph_builder import (
    PROMPT_PROFILES,
    materialize_milestone_subgraph,
)
from orchestra.roles.pool import RolePool, default_role_pool
from orchestra.roles.templates import SubgraphTemplate, TemplateSlot, default_templates


class PlanRecompileError(ValueError):
    """Raised when a milestone cannot be recompiled into the requested shape."""


@dataclass(frozen=True)
class TemplateSwitch:
    """The shape a playbook wants, and who fills the slots it cares about.

    ``slots`` names a role per slot id. A slot the switch does not name keeps the
    role the parent milestone gave the slot of that name, and falls back to the
    slot's own default when the parent had no such slot. Optional slots are left
    empty unless named: a template's optional slot describes what it *can* run,
    and filling one nobody asked for spends a budget nobody asked for.
    """

    template_id: str
    slots: Mapping[str, str] = field(default_factory=dict)
    playbook_id: str = ""
    reason: str = ""


def milestone_draft_for(graph: OrchestraGraph) -> MilestoneDraft:
    """The milestone this graph was compiled from.

    A compiled graph does not contain its milestone: the objective, the
    acceptance checks and each agent's mandate go into contract prompts and are
    not recoverable from the nodes. ``materialize_milestone_subgraph`` writes the
    draft beside the graph and records the path, so a graph compiled by any other
    route cannot be recompiled and says so.
    """
    path = str((graph.metadata or {}).get("milestone_draft_path") or "")
    if not path:
        raise PlanRecompileError(
            f"graph {graph.graph_id!r} records no milestone draft, so there is "
            "nothing to recompile from"
        )
    if not Path(path).is_file():
        raise PlanRecompileError(f"recorded milestone draft is missing: {path}")
    return _draft_from_payload(json.loads(Path(path).read_text(encoding="utf-8")))


def recompile_candidate(
    *,
    base_graph: OrchestraGraph,
    switch: TemplateSwitch,
    candidate_id: str,
    generation_reason: str = "",
    pool: RolePool | None = None,
    templates: Mapping[str, SubgraphTemplate] | None = None,
) -> LocalCandidate:
    """Compile ``base_graph``'s milestone into ``switch``'s shape.

    The new contracts and graph are written under a namespace of their own, so a
    candidate never overwrites the attempt it is being compared against. Because
    the contracts land in the directory the run's compiler was built from, a
    caller that validates candidates against a ``GraphCompiler`` has to rebuild
    it after this returns — the registry is read once at construction.
    """
    role_pool = pool or default_role_pool()
    catalog = templates or default_templates()
    template = catalog.get(switch.template_id)
    if template is None:
        raise PlanRecompileError(f"unknown template {switch.template_id!r}")

    draft = milestone_draft_for(base_graph)
    metadata = dict(base_graph.metadata or {})
    agents, assignment, changed = _fill_slots(
        draft=draft, template=template, switch=switch, pool=role_pool
    )
    if not changed and template.template_id == draft.template_id:
        raise PlanRecompileError(
            f"recompiling {draft.milestone_id!r} as {template.template_id!r} "
            "would reproduce its parent, which is a candidate that cannot win "
            "and cannot lose"
        )

    graph_path, _roster = materialize_milestone_subgraph(
        generated_root=_generated_root(metadata),
        milestone=replace(draft, template_id=template.template_id, agents=agents),
        agent_backend=str(metadata.get("agent_backend") or ""),
        harness_command=_harness_command(base_graph),
        model_name=_model_name(base_graph),
        profile=_prompt_profile(metadata),
        benchmark=str(metadata.get("benchmark") or "realbench"),
        harness_timeout_seconds=_harness_timeout(base_graph),
        contract_namespace=candidate_id,
    )

    graph = load_graph(graph_path)
    graph.metadata = {
        **dict(graph.metadata),
        "parent_graph_hash": base_graph.content_hash,
    }
    return LocalCandidate(
        candidate_id=candidate_id,
        parent_graph_hash=base_graph.content_hash,
        # Empty on purpose: nothing was edited. `plan_recompile` is what
        # describes this candidate, and the frontier reads it there.
        edits=[],
        graph=graph,
        generation_reason=generation_reason or switch.reason,
        playbook_id=switch.playbook_id,
        plan_recompile=PlanRecompile(
            template_id=template.template_id,
            parent_template_id=draft.template_id,
            slots=assignment,
            changed_slots=changed,
            contract_namespace=candidate_id,
        ),
    )


def register_new_contracts(
    *,
    compiler: GraphCompiler,
    graph: OrchestraGraph,
    contracts_dir: str | Path,
    executor_contracts: dict[str, AgentContract] | None = None,
) -> bool:
    """Load contracts ``graph`` names that the run's registries have not seen.

    A recompiled milestone writes one fresh contract per agent into the run's
    contracts directory — the same directory the compiler read once, when it was
    built. Without this the candidate fails to compile and is recorded as a
    rejection, which reads as an illegal design rather than a stale registry.

    The executor keeps its own copy, loaded at CLI start. Updating only the
    compiler lets the candidate compile and then die at runtime with a KeyError
    on the new ``contract_id``. Both maps are updated in place so every holder
    sees the same registry. Returns whether anything was loaded; nothing is
    read on the common path where a graph names only contracts already known.
    """
    named = {
        node.contract_id
        for node in graph.nodes
        if node.node_kind is NodeKind.AGENT and getattr(node, "contract_id", "")
    }
    compiler_stale = not named <= set(compiler.contracts)
    executor_stale = executor_contracts is not None and not named <= set(
        executor_contracts
    )
    if not compiler_stale and not executor_stale:
        return False
    loaded = load_contracts(str(contracts_dir))
    compiler.contracts.update(loaded)
    if executor_contracts is not None:
        executor_contracts.update(loaded)
    return True


def _fill_slots(
    *,
    draft: MilestoneDraft,
    template: SubgraphTemplate,
    switch: TemplateSwitch,
    pool: RolePool,
) -> tuple[list[AgentDraft], dict[str, str], list[str]]:
    """One agent per instantiated slot, plus the assignment and what changed."""
    unknown = set(switch.slots) - {slot.slot_id for slot in template.slots}
    if unknown:
        raise PlanRecompileError(
            f"template {template.template_id!r} has no slots {sorted(unknown)}"
        )
    inherited = {agent.slot_id: agent for agent in draft.agents if agent.slot_id}

    agents: list[AgentDraft] = []
    assignment: dict[str, str] = {}
    changed: list[str] = []
    used: set[str] = set()
    for slot in template.slots:
        parent = inherited.get(slot.slot_id)
        requested = switch.slots.get(slot.slot_id)
        if requested is None and parent is None and not slot.required:
            continue
        role_id = _role_for_slot(slot, requested=requested, parent=parent, pool=pool)
        assignment[slot.slot_id] = role_id
        if parent is None or parent.role != role_id:
            changed.append(slot.slot_id)

        node_id = f"{slot.slot_id}_{role_id}"[:48]
        while node_id in used:
            node_id = f"{node_id}_{len(used) + 1}"[:48]
        used.add(node_id)
        agents.append(
            _agent_draft(
                draft=draft,
                node_id=node_id,
                slot=slot,
                role_id=role_id,
                parent=parent if parent is not None and parent.role == role_id else None,
                pool=pool,
            )
        )

    if not agents:
        raise PlanRecompileError(
            f"template {template.template_id!r} came out with no agents"
        )
    _require_a_consumer_for_the_early_gate(template, assignment)
    _require_an_editing_terminal(agents, pool)
    return agents, assignment, changed


def _role_for_slot(
    slot: TemplateSlot,
    *,
    requested: str | None,
    parent: AgentDraft | None,
    pool: RolePool,
) -> str:
    """A named role is validated and never silently replaced.

    The planner's own slot filling falls back to the default when it is handed an
    illegal role, because a planner is an LLM and a plan that degrades is better
    than one that fails. A playbook is code: a role it names and does not get is a
    bug, and swallowing it would hide the search running a shape nobody chose.
    """
    if requested is not None:
        if pool.get(requested) is None:
            raise PlanRecompileError(f"unknown role {requested!r}")
        if not slot.accepts(requested):
            raise PlanRecompileError(
                f"slot {slot.slot_id!r} does not accept role {requested!r}; "
                f"it allows {sorted(slot.allowed_roles) or 'any role'}"
            )
        return requested
    if parent is not None and pool.get(parent.role) is not None and slot.accepts(parent.role):
        return parent.role
    return slot.default_role


def _agent_draft(
    *,
    draft: MilestoneDraft,
    node_id: str,
    slot: TemplateSlot,
    role_id: str,
    parent: AgentDraft | None,
    pool: RolePool,
) -> AgentDraft:
    """Carry the parent agent's mandate and budgets, or fall back to the role's.

    ``parent`` is passed only when the slot kept both its name and its role.
    A mandate was written for one role in one position, so carrying it onto a
    different role would tell, say, a test author to implement the milestone.
    """
    role = pool.require(role_id)
    if parent is not None:
        return replace(parent, role_id=node_id, slot_id=slot.slot_id)
    return AgentDraft(
        role_id=node_id,
        title=role.title,
        mandate=(
            f"Carry out your role for this milestone: {draft.objective}"
            if draft.objective
            else f"Carry out your role for milestone {draft.milestone_id}."
        )[:4000],
        role=role_id,
        slot_id=slot.slot_id,
        focus_paths=list(draft.focus_paths),
        max_tokens=role.max_tokens,
        max_steps=role.max_steps,
        timeout_seconds=role.timeout_seconds,
    )


def _require_a_consumer_for_the_early_gate(
    template: SubgraphTemplate, assignment: Mapping[str, str]
) -> None:
    """A template that gates early needs the slot waiting behind the failure.

    Without it the builder gates once at the end as usual, which compiles to the
    parent's shape under a new template name. That candidate is not rejected
    anywhere downstream: it runs, scores, and is recorded as though a different
    design had been measured.
    """
    if not template.early_gate_after:
        return
    repair_slots = [slot.slot_id for slot in template.slots if slot.runs_if_gate_failed]
    if repair_slots and not any(slot_id in assignment for slot_id in repair_slots):
        raise PlanRecompileError(
            f"{template.template_id!r} gates after {template.early_gate_after!r} "
            f"but no slot in {sorted(repair_slots)} was filled, so the gate would "
            "have nothing behind it and the shape would collapse to its parent's"
        )


def _require_an_editing_terminal(agents: list[AgentDraft], pool: RolePool) -> None:
    """The compiler freezes the last agent it instantiates, so it cannot be read-only.

    A reviewer in the terminal position hands the acceptance harness an empty
    diff to grade, and the milestone is scored on a change nobody made. The
    invariant check catches it too, but only once the graph exists and only by
    node id; here the slot a switch failed to fill can still be named.
    """
    last = agents[-1]
    role = pool.get(last.role)
    if role is not None and not role.edits_repository:
        raise PlanRecompileError(
            f"slot {last.slot_id!r} holds the read-only role {last.role!r} and is "
            "the last agent, so the acceptance harness would grade an empty diff; "
            "an editing slot has to come after it"
        )


def _draft_from_payload(payload: dict) -> MilestoneDraft:
    acceptance = payload.get("acceptance") or {}
    return MilestoneDraft(
        milestone_id=str(payload["milestone_id"]),
        title=str(payload.get("title") or ""),
        objective=str(payload.get("objective") or ""),
        risk_rationale=str(payload.get("risk_rationale") or ""),
        gate_level=payload["gate_level"],
        split_reason=payload.get("split_reason") or "risk_gate",
        template_id=str(payload.get("template_id") or ""),
        depends_on=list(payload.get("depends_on") or []),
        focus_paths=list(payload.get("focus_paths") or []),
        acceptance=MilestoneAcceptance(
            criteria=list(acceptance.get("criteria") or []),
            corner_cases=list(acceptance.get("corner_cases") or []),
            checks=list(acceptance.get("checks") or []),
        ),
        agents=[AgentDraft(**agent) for agent in payload.get("agents") or []],
    )


def _generated_root(metadata: Mapping[str, object]) -> Path:
    root = str(metadata.get("generated_root") or "")
    if not root:
        raise PlanRecompileError(
            "graph records no generated root, so a recompilation has nowhere to "
            "write contracts the run's compiler will find"
        )
    return Path(root)


def _prompt_profile(metadata: Mapping[str, object]):  # noqa: ANN202 - DatasetPromptProfile
    """Refuse to guess: the wrong profile points agents at documents they lack."""
    profile_id = str(metadata.get("prompt_profile") or "")
    profile = PROMPT_PROFILES.get(profile_id)
    if profile is None:
        raise PlanRecompileError(
            f"graph names prompt profile {profile_id!r}, which is not one of "
            f"{sorted(PROMPT_PROFILES)}"
        )
    return profile


def _harness_nodes(graph: OrchestraGraph):  # noqa: ANN202 - list[NodeSpec]
    return [node for node in graph.nodes if node.node_kind is NodeKind.HARNESS]


def _harness_command(graph: OrchestraGraph) -> list[str]:
    """The parent's command verbatim, which is what pins the frozen suite.

    The authored suite lives outside the workspace and is named by ``--spec-tests``
    in this command. Rebuilding the command instead of copying it would let a
    candidate be scored against a different suite than its parent, and the
    comparison between them would mean nothing.
    """
    for node in _harness_nodes(graph):
        command = list(getattr(node, "command", None) or [])
        if command:
            return command
    raise PlanRecompileError(f"graph {graph.graph_id!r} has no harness command")


def _harness_timeout(graph: OrchestraGraph) -> int:
    for node in _harness_nodes(graph):
        timeout = getattr(node, "timeout_seconds", None)
        if timeout:
            return int(timeout)
    return 180


def _model_name(graph: OrchestraGraph) -> str | None:
    for node in graph.nodes:
        model = getattr(node, "model", None)
        if model is not None and getattr(model, "name", ""):
            return str(model.name)
    return None


__all__ = [
    "PlanRecompileError",
    "TemplateSwitch",
    "milestone_draft_for",
    "recompile_candidate",
    "register_new_contracts",
]
