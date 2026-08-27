"""Subgraph templates a planner instantiates instead of inventing a topology.

Every milestone used to compile to the same shape — a straight chain of agents
ending at the acceptance gate — so the only thing a planner could vary was how
many agents to put in the chain. These templates make the shape itself a
decision, drawn from a fixed catalogue (EvoMAS, arXiv:2605.08769) rather than
generated per task.

The catalogue is deliberately restricted to what the runtime can actually run:

* Agents in a milestone share one workspace, so two agents that both edit may
  never sit in the same parallel wave. Only ``edits_repository: false`` roles
  are allowed to fan out.
* The graph compiler rejects cycles, so there are no retry loops. A template
  that wants "try, then fix" spends a second slot on it.
* Several conditional edges may feed one input slot, and the first active one
  with a payload wins. That is what lets a gate run early and skip the repair
  slot entirely when it passes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from orchestra.roles.pool import RolePool

DEFAULT_TEMPLATE_DIR = Path("configs/subgraph_templates")

FALLBACK_TEMPLATE_ID = "chain"


class SubgraphTemplateError(ValueError):
    """Raised when a template definition on disk is unusable."""


@dataclass(frozen=True)
class TemplateSlot:
    """One agent position in a template, filled by a role from the pool."""

    slot_id: str
    default_role: str
    allowed_roles: list[str] = field(default_factory=list)
    required: bool = True
    # A slot whose inputs include the gate report only becomes runnable when the
    # early gate fails, so an easy milestone never pays for it.
    runs_if_gate_failed: bool = False

    def accepts(self, role_id: str) -> bool:
        return not self.allowed_roles or role_id in self.allowed_roles


@dataclass(frozen=True)
class SubgraphTemplate:
    """A topology over slots, plus where the acceptance gate sits."""

    template_id: str
    title: str
    when_to_use: str
    slots: list[TemplateSlot]
    edges: list[tuple[str, str]]
    early_gate_after: str | None = None
    # A chain can absorb more agents than it declares slots for. Frozen plans
    # rely on this: the merged control arm of an A/B test concentrates every
    # agent of a multi-milestone plan into one milestone, and dropping the
    # overflow would silently hand that arm less compute.
    extensible: bool = False
    # Whether the planner is offered this shape when it decomposes a task. A
    # template exists for two audiences: the planner choosing a shape up front,
    # and a playbook recompiling a milestone that already failed. Offering a new
    # shape to the planner changes the distribution of plans, so a run comparing
    # search against no search would be measuring two changes at once. Templates
    # added for the search alone therefore stay out of the catalogue until the
    # search has shown they are worth having.
    planner_selectable: bool = True

    def slots_for(self, count: int) -> list[TemplateSlot]:
        """Slots to instantiate for ``count`` agents, extending a chain if asked."""
        if count <= len(self.slots) or not self.extensible:
            return list(self.slots)
        tail = self.slots[-1]
        extra = [
            TemplateSlot(
                slot_id=f"{tail.slot_id}_{index + 2}",
                default_role=tail.default_role,
                allowed_roles=list(tail.allowed_roles),
                required=False,
                runs_if_gate_failed=tail.runs_if_gate_failed,
            )
            for index in range(count - len(self.slots))
        ]
        return [*self.slots, *extra]

    def edges_for(self, slots: list[TemplateSlot]) -> list[tuple[str, str]]:
        """Template edges plus the chain links joining any extended slots."""
        edges = list(self.edges)
        known = {slot.slot_id for slot in self.slots}
        previous = self.slots[-1].slot_id
        for slot in slots:
            if slot.slot_id in known:
                continue
            edges.append((previous, slot.slot_id))
            previous = slot.slot_id
        return edges

    @property
    def agent_count(self) -> int:
        return len(self.slots)

    def slot(self, slot_id: str) -> TemplateSlot:
        for item in self.slots:
            if item.slot_id == slot_id:
                return item
        raise SubgraphTemplateError(f"unknown slot {slot_id!r} in {self.template_id}")

    def terminal_slot(self) -> TemplateSlot:
        """The slot whose change is frozen when every slot runs."""
        destinations = {dst for _, dst in self.edges}
        tail = [slot for slot in self.slots if slot.slot_id not in {src for src, _ in self.edges}]
        if len(tail) == 1:
            return tail[0]
        # Fall back to declaration order when the shape is a fan-in.
        for slot in reversed(self.slots):
            if slot.slot_id in destinations or len(self.slots) == 1:
                return slot
        return self.slots[-1]

    def layers(self) -> list[list[str]]:
        """Slots grouped into execution waves, so a fan-out reads as one."""
        depth: dict[str, int] = {slot.slot_id: 0 for slot in self.slots}
        for _ in range(len(self.slots)):
            changed = False
            for src, dst in self.edges:
                if depth[dst] < depth[src] + 1:
                    depth[dst] = depth[src] + 1
                    changed = True
            if not changed:
                break
        grouped: dict[int, list[str]] = {}
        for slot in self.slots:
            grouped.setdefault(depth[slot.slot_id], []).append(slot.slot_id)
        return [grouped[key] for key in sorted(grouped)]

    def catalog_entry(self) -> str:
        shape = " -> ".join(
            layer[0] if len(layer) == 1 else "{" + " | ".join(layer) + "}"
            for layer in self.layers()
        )
        gate = (
            f" Gate runs after `{self.early_gate_after}` and the remaining slots "
            "are skipped when it passes."
            if self.early_gate_after
            else ""
        )
        return (
            f"- `{self.template_id}` ({self.agent_count} agent"
            f"{'s' if self.agent_count != 1 else ''}: {shape}): "
            f"{self.when_to_use.strip()}{gate}"
        )


def _parse_slot(payload: Any, *, template_id: str) -> TemplateSlot:
    if not isinstance(payload, dict):
        raise SubgraphTemplateError(f"{template_id}: slot must be a mapping")
    slot_id = str(payload.get("id") or payload.get("slot_id") or "").strip()
    if not slot_id:
        raise SubgraphTemplateError(f"{template_id}: slot needs an id")
    default_role = str(payload.get("default_role") or "").strip()
    if not default_role:
        raise SubgraphTemplateError(f"{template_id}.{slot_id}: default_role is required")
    allowed = [str(item).strip() for item in (payload.get("allowed_roles") or [])]
    return TemplateSlot(
        slot_id=slot_id,
        default_role=default_role,
        allowed_roles=[item for item in allowed if item],
        required=bool(payload.get("required", True)),
        runs_if_gate_failed=bool(payload.get("runs_if_gate_failed", False)),
    )


def parse_template(payload: Any, *, source: str = "<memory>") -> SubgraphTemplate:
    if not isinstance(payload, dict):
        raise SubgraphTemplateError(f"{source}: template must be a mapping")
    template_id = str(payload.get("template_id") or "").strip()
    if not template_id:
        raise SubgraphTemplateError(f"{source}: template_id is required")
    slots = [
        _parse_slot(item, template_id=template_id) for item in (payload.get("slots") or [])
    ]
    if not slots:
        raise SubgraphTemplateError(f"{template_id}: needs at least one slot")
    slot_ids = {slot.slot_id for slot in slots}
    if len(slot_ids) != len(slots):
        raise SubgraphTemplateError(f"{template_id}: duplicate slot ids")
    edges: list[tuple[str, str]] = []
    for item in payload.get("edges") or []:
        if not isinstance(item, dict):
            raise SubgraphTemplateError(f"{template_id}: edge must be a mapping")
        src = str(item.get("from") or "").strip()
        dst = str(item.get("to") or "").strip()
        if src not in slot_ids or dst not in slot_ids:
            raise SubgraphTemplateError(
                f"{template_id}: edge {src!r}->{dst!r} names an unknown slot"
            )
        edges.append((src, dst))
    early = payload.get("early_gate_after")
    early_gate_after = str(early).strip() if early else None
    if early_gate_after and early_gate_after not in slot_ids:
        raise SubgraphTemplateError(
            f"{template_id}: early_gate_after names an unknown slot {early_gate_after!r}"
        )
    if early_gate_after and not any(slot.runs_if_gate_failed for slot in slots):
        raise SubgraphTemplateError(
            f"{template_id}: an early gate is pointless unless some slot is "
            "marked runs_if_gate_failed"
        )
    return SubgraphTemplate(
        template_id=template_id,
        title=str(payload.get("title") or template_id).strip(),
        when_to_use=str(payload.get("when_to_use") or "").strip(),
        slots=slots,
        edges=edges,
        early_gate_after=early_gate_after,
        extensible=bool(payload.get("extensible", False)),
        planner_selectable=bool(payload.get("planner_selectable", True)),
    )


def validate_against_pool(template: SubgraphTemplate, pool: RolePool) -> None:
    """Reject a template whose roles do not exist or whose fan-out would race.

    Two editing agents in the same parallel wave would write the same working
    tree at the same time, so a template that fans out to them is not a
    topology choice, it is a corrupted workspace.
    """
    for slot in template.slots:
        for role_id in {slot.default_role, *slot.allowed_roles}:
            if role_id not in pool:
                raise SubgraphTemplateError(
                    f"{template.template_id}.{slot.slot_id}: unknown role {role_id!r}"
                )
    by_source: dict[str, list[str]] = {}
    for src, dst in template.edges:
        by_source.setdefault(src, []).append(dst)
    for src, destinations in by_source.items():
        if len(destinations) < 2:
            continue
        writers = [
            dst
            for dst in destinations
            if any(
                pool.require(role).edits_repository
                for role in {
                    template.slot(dst).default_role,
                    *template.slot(dst).allowed_roles,
                }
            )
        ]
        if writers:
            raise SubgraphTemplateError(
                f"{template.template_id}: slots {sorted(writers)} run in parallel "
                "after "
                f"{src!r} but may edit the shared workspace"
            )


def load_templates(
    directory: str | Path = DEFAULT_TEMPLATE_DIR, *, pool: RolePool | None = None
) -> dict[str, SubgraphTemplate]:
    root = Path(directory)
    if not root.is_dir():
        raise SubgraphTemplateError(f"template directory not found: {root}")
    templates: dict[str, SubgraphTemplate] = {}
    for path in sorted(root.glob("*.yaml")):
        template = parse_template(
            yaml.safe_load(path.read_text(encoding="utf-8")), source=str(path)
        )
        if template.template_id in templates:
            raise SubgraphTemplateError(f"duplicate template_id in {path}")
        if pool is not None:
            validate_against_pool(template, pool)
        templates[template.template_id] = template
    if FALLBACK_TEMPLATE_ID not in templates:
        raise SubgraphTemplateError(
            f"the fallback template {FALLBACK_TEMPLATE_ID!r} must exist in {root}"
        )
    return templates


@lru_cache(maxsize=4)
def default_templates(
    directory: str | Path = DEFAULT_TEMPLATE_DIR,
) -> dict[str, SubgraphTemplate]:
    return load_templates(directory)


def catalog_lines(templates: dict[str, SubgraphTemplate]) -> list[str]:
    """The shapes a planner may choose from, which is not every shape that exists."""
    return [
        templates[key].catalog_entry()
        for key in sorted(templates, key=lambda k: (templates[k].agent_count, k))
        if templates[key].planner_selectable
    ]
