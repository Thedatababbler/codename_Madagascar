"""Playbooks keyed on why the search ran, then on class and shape.

Failure search and quality search are different questions and have different
tables. A failure playbook assumes the gate did not pass. A quality playbook
assumes it did, and that the behavioural score was still poor. Mixing them
sends a repairer that only runs behind a failing gate into a search whose
premise is a passing gate.

A playbook is a named recipe, not a compiled candidate. Edit-layer ones become
``LocalEdit``s once they know which node to attach to; plan-layer ones become a
``TemplateSwitch``. The table is the whole reachable design space: the generator
picks from it, it does not invent.

The feedback-only anchor is not in either catalogue. It is always index zero of
the draft list and does not count as a playbook — without it there is no way to
say what a playbook bought.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from orchestra.control.fast_loop.plan_candidates import (
    PlanRecompileError,
    TemplateSwitch,
    milestone_draft_for,
)
from orchestra.control.fast_loop.schemas import (
    BudgetAdjustmentEdit,
    FailureDiagnosis,
    LocalEdit,
    PromptFeedbackEdit,
)
from orchestra.control.task_state import SubtaskFailureReason
from orchestra.ir.graph import OrchestraGraph
from orchestra.ir.nodes import NodeKind
from orchestra.roles.pool import RolePool, default_role_pool


class FailureClass(StrEnum):
    BUDGET = "budget"
    FUNCTIONAL = "functional"
    DESIGN = "design"


class SearchReason(StrEnum):
    """Why the fast loop is running, which is not why the milestone failed."""

    FAILURE = "failure"
    QUALITY = "quality"


class PlaybookBindError(ValueError):
    """Raised when a playbook cannot be bound to this graph."""


_FUNCTIONAL = frozenset({FailureClass.FUNCTIONAL, FailureClass.DESIGN})
_BUDGET = frozenset({FailureClass.BUDGET})

_REVIEWER_CYCLE = ("contract_critic", "spec_auditor", "behaviour_critic")

_ROLE_FOR_STAGE: Mapping[str, str] = {
    "compile": "dependency_resolver",
    "imports": "dependency_resolver",
    "contracts": "contract_author",
    "tests": "edge_case_hardener",
    "spec_tests": "edge_case_hardener",
}

_MINIMUM_PASSING = (
    "Reach a minimum passing implementation first. Do not polish, refactor, "
    "or add features the gate did not name."
)

_QUALITY_GUARD = (
    "Do not delete spec_tests, check_tests, or documented public symbols. "
    "Do not reconstruct hidden tests. Improve the named behaviours only."
)


@dataclass(frozen=True)
class Playbook:
    """One named repair, either a parameter change or a shape change."""

    playbook_id: str
    reason: str
    classes: frozenset[FailureClass]
    #: Parent templates this applies to. Empty means "any template that has no
    #: more specific entry" — the fallback used when the current shape has no
    #: table of its own.
    templates: frozenset[str] = frozenset()
    #: Slot to attach an edit to. Empty means the diagnosis anchor.
    target: str = ""
    include_failure_list: bool = False
    steps_delta: int = 0
    timeout_delta: int = 0
    extra_prompt: str = ""
    #: After a plan-layer recompile, attach the failure list to these slots of
    #: the *new* graph. Empty means ``target`` when that is set.
    feedback_slots: tuple[str, ...] = ()
    switch_template: str = ""
    switch_slots: tuple[tuple[str, str], ...] = ()
    #: Copy a parent slot's role onto a differently named slot of the target
    #: template. Same-name slots inherit on their own; this is for solo→chain
    #: where ``author`` becomes ``first``.
    carry: tuple[tuple[str, str], ...] = ()
    stage_slot: str = ""
    swap_reviewer: bool = False
    #: Slots of the target template whose role comes from the diagnosis rather
    #: than from this row: ``recommended_role`` (an editing role) fills
    #: ``role_from_diagnosis``, ``recommended_reviewer`` fills
    #: ``reviewer_from_diagnosis``. Empty recommendation keeps the row's own
    #: default, and a role the target slot does not accept is ignored rather
    #: than sent to the recompile, which would reject the whole candidate.
    role_from_diagnosis: str = ""
    reviewer_from_diagnosis: str = ""
    #: Which search this row belongs to. Default is failure: existing rows were
    #: written for a gate that did not pass. Quality rows must opt in.
    search_reasons: frozenset[SearchReason] = field(
        default_factory=lambda: frozenset({SearchReason.FAILURE})
    )

    @property
    def layer(self) -> str:
        return "plan" if self.switch_template else "edit"


@dataclass
class PlaybookContext:
    """What a playbook needs from the failed attempt in order to bind."""

    graph: OrchestraGraph
    diagnosis: FailureDiagnosis
    failure_class: FailureClass
    template_id: str
    anchor_node_id: str
    behaviour_failures: list[str]
    pool: RolePool
    search_reason: SearchReason = SearchReason.FAILURE
    nodes_by_slot: dict[str, str] = field(default_factory=dict)
    roles_by_slot: dict[str, str] = field(default_factory=dict)

    def node_for(self, slot: str) -> str | None:
        if not slot:
            return self.anchor_node_id
        return self.nodes_by_slot.get(slot)

    def role_for(self, slot: str) -> str | None:
        return self.roles_by_slot.get(slot)


def infer_failure_class(diagnosis: FailureDiagnosis) -> FailureClass:
    """The class the table keys on, until an LLM writes ``failure_class``.

    A lookup, not a judgement: timeout and an early harness stage are budget;
    a harness that reached the tests is functional. ``design`` is left to a
    classifier that can see spread and history — inventing it here would send
    every leftover into the class the search is least able to act on.
    """
    named = str(diagnosis.failure_class or "").strip()
    try:
        return FailureClass(named)
    except ValueError:
        pass
    if diagnosis.reason is SubtaskFailureReason.TIMEOUT:
        return FailureClass.BUDGET
    stage = str(diagnosis.furthest_stage or "").lower()
    if stage in {"compile", "imports"}:
        return FailureClass.BUDGET
    return FailureClass.FUNCTIONAL


def catalog_for(
    search_reason: SearchReason,
    catalog: tuple[Playbook, ...] | None = None,
) -> tuple[Playbook, ...]:
    """The table for this search, or an explicit override.

    Quality and failure do not share a default table. An override is for tests
    that want a stub catalogue, and is still filtered by ``search_reasons``.
    """
    if catalog is not None:
        return catalog
    if search_reason is SearchReason.QUALITY:
        return QUALITY_CATALOG
    return CATALOG


def playbooks_for(
    template_id: str,
    failure_class: FailureClass | None = None,
    *,
    search_reason: SearchReason = SearchReason.FAILURE,
    catalog: tuple[Playbook, ...] | None = None,
) -> list[Playbook]:
    """The ordered playbooks for this search, shape, and (on failure) class.

    Quality search ignores ``failure_class``: the class is a failure diagnosis,
    and a passing milestone has none. A template with its own rows hides the
    generic fallbacks, so a ``test_first`` functional failure gets the
    repairer-targeted list rather than the same idea aimed at whichever agent
    happened to fail.
    """
    rows = [
        playbook
        for playbook in catalog_for(search_reason, catalog)
        if search_reason in playbook.search_reasons
    ]
    if search_reason is SearchReason.QUALITY:
        specific = [
            playbook
            for playbook in rows
            if playbook.templates and template_id in playbook.templates
        ]
        if specific:
            return specific
        return [playbook for playbook in rows if not playbook.templates]
    if failure_class is None:
        raise TypeError("failure_class is required when search_reason is failure")
    specific = [
        playbook
        for playbook in rows
        if playbook.templates
        and template_id in playbook.templates
        and failure_class in playbook.classes
    ]
    if specific:
        return specific
    return [
        playbook
        for playbook in rows
        if not playbook.templates and failure_class in playbook.classes
    ]


def context_for(
    *,
    graph: OrchestraGraph,
    diagnosis: FailureDiagnosis,
    anchor_node_id: str,
    pool: RolePool | None = None,
    search_reason: SearchReason = SearchReason.FAILURE,
) -> PlaybookContext:
    role_pool = pool or default_role_pool()
    template_id = template_id_of(graph)
    nodes, roles = _slot_maps(graph, role_pool)
    return PlaybookContext(
        graph=graph,
        diagnosis=diagnosis,
        failure_class=infer_failure_class(diagnosis),
        template_id=template_id,
        anchor_node_id=anchor_node_id,
        behaviour_failures=list(diagnosis.behaviour_failures),
        pool=role_pool,
        search_reason=search_reason,
        nodes_by_slot=nodes,
        roles_by_slot=roles,
    )


def template_id_of(graph: OrchestraGraph) -> str:
    recorded = str((graph.metadata or {}).get("template_id") or "")
    if recorded:
        return recorded
    try:
        return milestone_draft_for(graph).template_id
    except PlanRecompileError:
        return ""


def playbook_applies(playbook: Playbook, ctx: PlaybookContext) -> bool:
    """Skip a playbook that would be a no-op on this graph."""
    # An edit-layer row whose whole content is the named list has nothing to
    # say without names. A plan-layer row changes the shape regardless; the
    # names ride along when the gate produced any (a compile failure names
    # none, and that is exactly when a shape with a dependency resolver is
    # the right move).
    if playbook.include_failure_list and not ctx.behaviour_failures and playbook.layer == "edit":
        return False
    if playbook.layer == "edit" and ctx.node_for(playbook.target) is None:
        return False
    if playbook.swap_reviewer:
        nxt = _next_reviewer(ctx.role_for("reviewer"))
        if nxt is None or nxt == ctx.role_for("reviewer"):
            return False
    if playbook.stage_slot:
        role = role_for_stage(ctx.diagnosis.furthest_stage)
        if ctx.role_for(playbook.stage_slot) == role:
            return False
    return True


def bind_edits(playbook: Playbook, ctx: PlaybookContext) -> list[LocalEdit]:
    if playbook.layer != "edit":
        raise PlaybookBindError(f"{playbook.playbook_id} is a plan-layer playbook")
    node_id = ctx.node_for(playbook.target)
    if node_id is None:
        raise PlaybookBindError(
            f"{playbook.playbook_id} has no node for slot {playbook.target or 'anchor'!r}"
        )
    edits: list[LocalEdit] = []
    if playbook.include_failure_list:
        if not ctx.behaviour_failures:
            raise PlaybookBindError(f"{playbook.playbook_id} has no named failures")
        formatter = (
            _format_quality_failures
            if ctx.search_reason is SearchReason.QUALITY
            else _format_failures
        )
        edits.append(
            PromptFeedbackEdit(
                node_id=node_id,
                feedback=formatter(ctx.behaviour_failures, ctx.diagnosis),
            )
        )
    if playbook.steps_delta or playbook.timeout_delta:
        edits.append(
            BudgetAdjustmentEdit(
                node_id=node_id,
                max_steps_delta=playbook.steps_delta,
                timeout_seconds_delta=playbook.timeout_delta,
            )
        )
    if playbook.extra_prompt:
        edits.append(PromptFeedbackEdit(node_id=node_id, feedback=playbook.extra_prompt))
    if not edits:
        raise PlaybookBindError(f"{playbook.playbook_id} bound to no edits")
    return edits


def bind_switch(playbook: Playbook, ctx: PlaybookContext) -> TemplateSwitch:
    if playbook.layer != "plan":
        raise PlaybookBindError(f"{playbook.playbook_id} is an edit-layer playbook")
    slots = dict(playbook.switch_slots)
    for parent_slot, target_slot in playbook.carry:
        role = ctx.role_for(parent_slot)
        if role and target_slot not in slots:
            slots[target_slot] = role
    if playbook.stage_slot:
        slots[playbook.stage_slot] = role_for_stage(ctx.diagnosis.furthest_stage)
    if playbook.swap_reviewer:
        nxt = _next_reviewer(ctx.role_for("reviewer"))
        if nxt is None:
            raise PlaybookBindError(f"{playbook.playbook_id} has no reviewer to swap")
        slots["reviewer"] = nxt
    for slot_id, role in (
        (playbook.role_from_diagnosis, ctx.diagnosis.recommended_role),
        (playbook.reviewer_from_diagnosis, ctx.diagnosis.recommended_reviewer),
    ):
        if slot_id and role and _target_accepts(playbook.switch_template, slot_id, role, ctx.pool):
            slots[slot_id] = role
    return TemplateSwitch(
        template_id=playbook.switch_template,
        slots=slots,
        playbook_id=playbook.playbook_id,
        reason=playbook.reason,
    )


def bind_recompile_feedback(playbook: Playbook, ctx: PlaybookContext) -> list[LocalEdit]:
    """Prompt notes for a plan-layer candidate, bound to the *new* graph.

    Recompile produces a different roster. Names and guards have to land on the
    slot that will actually run — an improver the parent never had — not on the
    parent builder the switch left behind.
    """
    slots = playbook.feedback_slots or ((playbook.target,) if playbook.target else ())
    if not slots:
        return []
    if not playbook.include_failure_list and not playbook.extra_prompt:
        return []
    edits: list[LocalEdit] = []
    formatter = (
        _format_quality_failures
        if ctx.search_reason is SearchReason.QUALITY
        else _format_failures
    )
    for slot in slots:
        node_id = ctx.node_for(slot)
        if node_id is None:
            raise PlaybookBindError(
                f"{playbook.playbook_id} has no node for slot {slot!r} on the "
                "recompiled graph"
            )
        if playbook.include_failure_list and ctx.behaviour_failures:
            edits.append(
                PromptFeedbackEdit(
                    node_id=node_id,
                    feedback=formatter(ctx.behaviour_failures, ctx.diagnosis),
                )
            )
        if playbook.extra_prompt:
            edits.append(PromptFeedbackEdit(node_id=node_id, feedback=playbook.extra_prompt))
    return edits


def _target_accepts(template_id: str, slot_id: str, role: str, pool: RolePool) -> bool:
    """Whether the target template's slot may hold ``role``; unknown means no."""
    if pool.get(role) is None:
        return False
    from orchestra.roles.templates import default_templates

    template = default_templates().get(template_id)
    if template is None:
        return False
    try:
        return template.slot(slot_id).accepts(role)
    except Exception:  # noqa: BLE001 -- an unknown slot is simply not filled
        return False


def role_for_stage(stage: str) -> str:
    return _ROLE_FOR_STAGE.get(str(stage or "").lower(), "edge_case_hardener")


def _next_reviewer(current: str | None) -> str | None:
    if current in _REVIEWER_CYCLE:
        index = _REVIEWER_CYCLE.index(current)
        return _REVIEWER_CYCLE[(index + 1) % len(_REVIEWER_CYCLE)]
    if current:
        return "behaviour_critic"
    return None


def _short_test_name(name: str) -> str:
    text = str(name).strip()
    if "::" in text:
        left, right = text.rsplit("::", 1)
        return f"{left.rsplit('.', 1)[-1]}::{right}"
    if "/" in text:
        return text.rsplit("/", 1)[-1]
    return text.rsplit(".", 1)[-1]


def _format_failures(
    names: list[str], diagnosis: FailureDiagnosis | None = None
) -> str:
    listed = "\n".join(f"- {_short_test_name(name)}" for name in names)
    return (
        "The acceptance gate failed these tests (names only; the suite is not "
        "in this repository):\n"
        f"{listed}\n"
        "Act on the behaviours these names state. Do not reconstruct the tests."
    )


def _format_quality_failures(
    names: list[str], diagnosis: FailureDiagnosis | None = None
) -> str:
    listed = "\n".join(f"- {_short_test_name(name)}" for name in names)
    named = len(names)
    total = diagnosis.behaviour_total if diagnosis is not None else None
    passed = diagnosis.behaviour_passed if diagnosis is not None else None
    count = ""
    samples = diagnosis.persistence_samples if diagnosis is not None else 0
    if total is not None and passed is not None:
        failed = max(0, total - passed)
        count = f" The suite ran {total} tests and {passed} passed."
        if samples > 1:
            count += (
                f" These {named} behaviours failed in every one of {samples} "
                "independent attempts at this milestone; behaviours that passed "
                "in at least one attempt are not listed. They are systematic, "
                "not flaky -- re-running will not fix them, a change will."
            )
        elif named and named < failed:
            count += (
                f" The gate named {named} of the {failed} failures; "
                "treat the list as incomplete."
            )
        elif named:
            count += f" Named failures ({named}):"
    return (
        "The acceptance gate passed, but these behaviours still fail (names "
        "only; the suite is not in this repository)."
        f"{count}\n"
        f"{listed}\n"
        "Fix these behaviours without regressing what already passes. Do not "
        "reconstruct the tests. Do not delete spec_tests, check_tests, or "
        "documented public symbols."
    )


def _slot_maps(graph: OrchestraGraph, pool: RolePool) -> tuple[dict[str, str], dict[str, str]]:
    """``slot -> compiled node id`` and ``slot -> pool role``."""
    nodes: dict[str, str] = {}
    roles: dict[str, str] = {}
    roster = list((graph.metadata or {}).get("agent_roster") or [])
    compiled = [n.node_id for n in graph.nodes if n.node_kind is NodeKind.AGENT]
    for entry in roster:
        if not isinstance(entry, Mapping):
            continue
        slot = str(entry.get("slot") or entry.get("slot_id") or "")
        if not slot:
            continue
        draft_id = str(entry.get("node_id") or entry.get("role_id") or "")
        role = str(entry.get("role") or "")
        node_id = _match_compiled(draft_id, compiled)
        if node_id:
            nodes[slot] = node_id
        if role:
            roles[slot] = role
        elif node_id:
            found = pool.role_for_node_id(node_id)
            if found is not None:
                roles[slot] = found.role_id
    if nodes:
        return nodes, roles
    try:
        draft = milestone_draft_for(graph)
    except PlanRecompileError:
        return nodes, roles
    for agent in draft.agents:
        if not agent.slot_id:
            continue
        node_id = _match_compiled(agent.role_id, compiled)
        if node_id:
            nodes[agent.slot_id] = node_id
            roles[agent.slot_id] = agent.role
    return nodes, roles


def _match_compiled(draft_id: str, compiled: list[str]) -> str | None:
    if not draft_id:
        return None
    if draft_id in compiled:
        return draft_id
    suffix = f"_{draft_id}"
    for node_id in compiled:
        if node_id.endswith(suffix):
            return node_id
    return None


CATALOG: tuple[Playbook, ...] = (
    # --- test_first --------------------------------------------------------
    Playbook(
        playbook_id="pb_tf_failures_to_repairer",
        reason="put the named failing tests into the repairer's prompt",
        classes=_FUNCTIONAL,
        templates=frozenset({"test_first", "test_first_diagnosed"}),
        target="repairer",
        include_failure_list=True,
    ),
    Playbook(
        playbook_id="pb_tf_diagnose_before_repair",
        reason="split the repair pass: a critic reads the names, then the repairer acts",
        classes=_FUNCTIONAL,
        templates=frozenset({"test_first"}),
        switch_template="test_first_diagnosed",
        switch_slots=(("critic", "behaviour_critic"),),
        # The pair comes from the diagnosis: the reviewer angle the failure
        # calls for, and the writer it should hand its report to. The
        # constants above are only what stands when nothing was diagnosed.
        reviewer_from_diagnosis="critic",
        role_from_diagnosis="repairer",
        include_failure_list=True,
        feedback_slots=("critic", "repairer"),
    ),
    Playbook(
        playbook_id="pb_tf_second_repairer",
        reason="two repair rounds behind the same failing gate",
        classes=_FUNCTIONAL,
        templates=frozenset({"test_first"}),
        switch_template="test_first_double_repair",
        switch_slots=(("second_repairer", "gate_repairer"),),
        role_from_diagnosis="second_repairer",
        include_failure_list=True,
        feedback_slots=("repairer", "second_repairer"),
    ),
    Playbook(
        playbook_id="pb_tf_builder_budget",
        reason="more steps and wall-clock on the builder",
        classes=_BUDGET,
        templates=frozenset({"test_first", "test_first_diagnosed"}),
        target="builder",
        steps_delta=2,
        timeout_delta=30,
    ),
    # --- gate_then_repair (the repair half of test_first) ------------------
    Playbook(
        playbook_id="pb_gtr_failures_to_repairer",
        reason="put the named failing tests into the repairer's prompt",
        classes=_FUNCTIONAL,
        templates=frozenset({"gate_then_repair"}),
        target="repairer",
        include_failure_list=True,
    ),
    Playbook(
        playbook_id="pb_gtr_author_budget",
        reason="more steps and wall-clock on the author",
        classes=_BUDGET,
        templates=frozenset({"gate_then_repair"}),
        target="author",
        steps_delta=2,
        timeout_delta=30,
    ),
    # --- solo --------------------------------------------------------------
    Playbook(
        playbook_id="pb_solo_to_gate_repair",
        reason="recompile as implement-gate-repair so a repairer costs nothing on a pass",
        classes=_FUNCTIONAL,
        templates=frozenset({"solo"}),
        switch_template="gate_then_repair",
        switch_slots=(("repairer", "gate_repairer"),),
        role_from_diagnosis="repairer",
        include_failure_list=True,
        feedback_slots=("repairer",),
    ),
    Playbook(
        playbook_id="pb_solo_to_review_fix",
        reason="recompile as review-then-fix with the reviewer and fixer the failure calls for",
        classes=_FUNCTIONAL,
        templates=frozenset({"solo"}),
        switch_template="review_then_fix",
        switch_slots=(
            ("reviewer", "behaviour_critic"),
            ("fixer", "gate_repairer"),
        ),
        reviewer_from_diagnosis="reviewer",
        role_from_diagnosis="fixer",
        include_failure_list=True,
        feedback_slots=("reviewer", "fixer"),
    ),
    Playbook(
        playbook_id="pb_solo_specialist",
        reason="recompile as a chain whose second slot is the specialist the failure calls for",
        classes=_FUNCTIONAL,
        templates=frozenset({"solo"}),
        switch_template="chain",
        carry=(("author", "first"),),
        # Formerly keyed on furthest_stage alone, which sent every test-stage
        # failure to the corner-case hardener. The stage still feeds the rule
        # floor (imports -> dependency_resolver); the residue is diagnosed.
        role_from_diagnosis="second",
        include_failure_list=True,
        feedback_slots=("second",),
    ),
    Playbook(
        playbook_id="pb_solo_budget",
        reason="more steps and wall-clock on the author",
        classes=_BUDGET,
        templates=frozenset({"solo"}),
        target="author",
        steps_delta=2,
        timeout_delta=30,
    ),
    # --- review_then_fix ---------------------------------------------------
    Playbook(
        playbook_id="pb_rtf_swap_angle",
        reason="same shape, the review angle the failure calls for (else the next unused one)",
        classes=_FUNCTIONAL,
        templates=frozenset({"review_then_fix"}),
        switch_template="review_then_fix",
        swap_reviewer=True,
        reviewer_from_diagnosis="reviewer",
        include_failure_list=True,
        feedback_slots=("reviewer", "fixer"),
    ),
    Playbook(
        playbook_id="pb_rtf_second_angle",
        reason="recompile as parallel_audit so two reviewers run at once",
        classes=_FUNCTIONAL,
        templates=frozenset({"review_then_fix"}),
        switch_template="parallel_audit",
        role_from_diagnosis="fixer",
        include_failure_list=True,
        feedback_slots=("fixer",),
    ),
    Playbook(
        playbook_id="pb_rtf_drop_reviewer",
        reason="drop the reviewer and spend the budget on the fixer",
        classes=_BUDGET,
        templates=frozenset({"review_then_fix"}),
        switch_template="chain",
        carry=(("author", "first"), ("fixer", "second")),
        include_failure_list=True,
        feedback_slots=("second",),
    ),
    # --- chain -------------------------------------------------------------
    Playbook(
        playbook_id="pb_chain_fill_third",
        reason="fill the unused third slot with the specialist the failure calls for",
        classes=_FUNCTIONAL,
        templates=frozenset({"chain"}),
        switch_template="chain",
        role_from_diagnosis="third",
        include_failure_list=True,
        feedback_slots=("third",),
    ),
    Playbook(
        playbook_id="pb_chain_to_review_fix",
        reason="put a reader between the two writers",
        classes=_FUNCTIONAL,
        templates=frozenset({"chain"}),
        switch_template="review_then_fix",
        carry=(("first", "author"), ("second", "fixer")),
        switch_slots=(("reviewer", "behaviour_critic"),),
        reviewer_from_diagnosis="reviewer",
        role_from_diagnosis="fixer",
        include_failure_list=True,
        feedback_slots=("reviewer", "fixer"),
    ),
    Playbook(
        playbook_id="pb_chain_budget",
        reason="more steps and wall-clock on the last writer",
        classes=_BUDGET,
        templates=frozenset({"chain"}),
        target="second",
        steps_delta=2,
        timeout_delta=30,
    ),
    # --- generic fallback, hidden by any template-specific row -------------
    Playbook(
        playbook_id="pb_failures_to_agent",
        reason="put the named failing tests into the anchored agent's prompt",
        classes=_FUNCTIONAL,
        include_failure_list=True,
    ),
    Playbook(
        playbook_id="pb_budget_steps_time_small",
        reason="small max_steps and timeout increase on the anchored agent",
        classes=_BUDGET,
        steps_delta=2,
        timeout_delta=30,
    ),
    Playbook(
        playbook_id="pb_budget_steps_time_large",
        reason="larger budget plus an instruction to reach a minimum pass first",
        classes=_BUDGET,
        steps_delta=8,
        timeout_delta=300,
        extra_prompt=_MINIMUM_PASSING,
    ),
)

_QUALITY = frozenset({SearchReason.QUALITY})
_NO_CLASS = frozenset()

QUALITY_CATALOG: tuple[Playbook, ...] = (
    # Cheap first: the gate already passed, so the next dollar has to change
    # what a writer sees or who writes. A repairer behind a failing gate is
    # the wrong person; an improver who never hears the names is a no-op.
    #
    # `pb_tf_q_failures_to_builder` used to sit here and was removed after
    # losing to the anchor three times out of three (-9.4pp, -31pp, and once
    # scoring identically to the incumbent). It differed from the anchor by
    # exactly one thing -- the named tests in the builder's prompt -- so those
    # are controlled measurements of that addition, and it does not pay. The
    # reason it cannot: `test_first` blinds the builder to the suite on
    # purpose, and the edit re-runs it FRESH, so the names do not repair the
    # named behaviours, they re-roll the whole substrate while biasing it
    # toward a handful of names out of a much larger graded set.
    Playbook(
        playbook_id="pb_tf_q_improve_after_gate",
        reason="recompile so an improver runs after the passing builder, told the named leaks",
        classes=_NO_CLASS,
        templates=frozenset({"test_first"}),
        target="improver",
        include_failure_list=True,
        extra_prompt=_QUALITY_GUARD,
        switch_template="test_first_improve",
        # The hardener is only the default. A diagnosis that names the specialist
        # the persistent failures call for overrides it (2026-08-31: semantic
        # misreads were handed to a corner-case hardener twice, by this constant).
        switch_slots=(("improver", "edge_case_hardener"),),
        role_from_diagnosis="improver",
        search_reasons=_QUALITY,
    ),
    Playbook(
        playbook_id="pb_tf_q_diagnose_then_improve",
        reason="a critic reads the named leaks, then an improver acts",
        classes=_NO_CLASS,
        templates=frozenset({"test_first"}),
        include_failure_list=True,
        extra_prompt=_QUALITY_GUARD,
        feedback_slots=("critic", "improver"),
        switch_template="test_first_quality_diagnosed",
        switch_slots=(
            ("critic", "behaviour_critic"),
            ("improver", "edge_case_hardener"),
        ),
        role_from_diagnosis="improver",
        reviewer_from_diagnosis="critic",
        search_reasons=_QUALITY,
    ),
    # Already on an improve shape: do not switch to the same shape again.
    Playbook(
        playbook_id="pb_tf_q_failures_to_improver",
        reason="put the named weak behaviours into the improver's prompt",
        classes=_NO_CLASS,
        templates=frozenset({"test_first_improve", "test_first_quality_diagnosed"}),
        target="improver",
        include_failure_list=True,
        extra_prompt=_QUALITY_GUARD,
        search_reasons=_QUALITY,
    ),
    # `pb_tf_q_improver_budget` and `pb_q_anchor_budget` were removed on
    # 2026-09-01: a quality search starts from a gate that passed, the Codex
    # backend reports step_count=1 for every run, and the only exit-signal
    # detector reads failure text a passing run does not have. A budget row
    # here could never be triggered by evidence, only by its position.
    Playbook(
        playbook_id="pb_tf_q_diagnose_from_improve",
        reason="split the improve pass: a critic reads the names, then the improver acts",
        classes=_NO_CLASS,
        templates=frozenset({"test_first_improve"}),
        include_failure_list=True,
        extra_prompt=_QUALITY_GUARD,
        feedback_slots=("critic", "improver"),
        switch_template="test_first_quality_diagnosed",
        switch_slots=(("critic", "behaviour_critic"),),
        reviewer_from_diagnosis="critic",
        search_reasons=_QUALITY,
    ),
    # Hidden by any template-specific row.
    Playbook(
        playbook_id="pb_q_failures_to_agent",
        reason="put the named weak behaviours into the anchored agent's prompt",
        classes=_NO_CLASS,
        include_failure_list=True,
        extra_prompt=_QUALITY_GUARD,
        search_reasons=_QUALITY,
    ),
)


__all__ = [
    "CATALOG",
    "QUALITY_CATALOG",
    "FailureClass",
    "Playbook",
    "PlaybookBindError",
    "PlaybookContext",
    "SearchReason",
    "bind_edits",
    "bind_recompile_feedback",
    "bind_switch",
    "catalog_for",
    "context_for",
    "infer_failure_class",
    "playbook_applies",
    "playbooks_for",
    "role_for_stage",
    "template_id_of",
]
