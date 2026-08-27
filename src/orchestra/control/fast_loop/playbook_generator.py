"""Candidate generator that drafts from the playbook table.

Sits beside ``DesignSearchCandidateGenerator`` and
``RuleBasedLocalCandidateGenerator``. Selecting this one is a call-site
decision; constructing the controller with ``design_search=True`` still gets
the atomic-edit generator.
"""

from __future__ import annotations

from collections.abc import Mapping

from orchestra.backends.capabilities import BackendCapabilities, SessionPolicy
from orchestra.control.fast_loop.candidate_generator import (
    MAX_LOCAL_CANDIDATES,
    _target_agent_id,
)
from orchestra.control.fast_loop.capability import filter_compatible_candidates
from orchestra.control.fast_loop.edit_engine import LocalEditError, apply_local_edits
from orchestra.control.fast_loop.plan_candidates import (
    PlanRecompileError,
    recompile_candidate,
)
from orchestra.control.fast_loop.playbooks import (
    Playbook,
    PlaybookBindError,
    PlaybookContext,
    bind_edits,
    bind_switch,
    context_for,
    playbook_applies,
    playbooks_for,
)
from orchestra.control.fast_loop.schemas import (
    CandidateRejectionReason,
    FailureDiagnosis,
    FastLoopBudget,
    LocalCandidate,
    LocalEdit,
    PromptFeedbackEdit,
    SessionPolicyEdit,
)
from orchestra.ir.compiler import GraphCompiler
from orchestra.ir.graph import OrchestraGraph
from orchestra.roles.pool import RolePool, default_role_pool
from orchestra.roles.templates import SubgraphTemplate


class PlaybookCandidateGenerator:
    """Index zero is the feedback-only anchor; the rest come from the table.

    Edit-layer playbooks share the same preamble the atomic generator uses —
    the failure report and a fresh session — so a playbook is measured against
    an attempt that saw the same evidence. Plan-layer playbooks recompile; the
    new shape's own gate report is the evidence those agents get.
    """

    def __init__(
        self,
        *,
        compiler: GraphCompiler | None = None,
        role_pool: RolePool | None = None,
        templates: Mapping[str, SubgraphTemplate] | None = None,
        catalog: tuple[Playbook, ...] | None = None,
    ) -> None:
        self.compiler = compiler
        self.role_pool = role_pool or default_role_pool()
        self.templates = templates
        self.catalog = catalog

    def generate(
        self,
        *,
        graph: OrchestraGraph,
        diagnosis: FailureDiagnosis,
        budget: FastLoopBudget,
        capabilities: Mapping[str, BackendCapabilities],
    ) -> list[LocalCandidate]:
        if diagnosis.infrastructure_related or not diagnosis.retryable:
            return []
        agent_id = _target_agent_id(graph, diagnosis)
        if agent_id is None:
            return []

        ctx = context_for(
            graph=graph,
            diagnosis=diagnosis,
            anchor_node_id=agent_id,
            pool=self.role_pool,
        )
        k = max(0, min(budget.max_candidates, MAX_LOCAL_CANDIDATES))
        parent_hash = graph.content_hash
        preamble: list[LocalEdit] = [
            PromptFeedbackEdit(node_id=agent_id, feedback=diagnosis.concise_feedback),
            SessionPolicyEdit(node_id=agent_id, policy=SessionPolicy.FRESH),
        ]

        built: list[LocalCandidate] = []
        if k >= 1:
            built.append(self._anchor(graph, preamble, parent_hash))

        for playbook in playbooks_for(ctx.template_id, ctx.failure_class, catalog=self.catalog):
            if len(built) >= k:
                break
            if not playbook_applies(playbook, ctx):
                continue
            built.append(self._from_playbook(playbook, ctx, preamble, parent_hash, budget))

        accepted, rejected = filter_compatible_candidates(built, capabilities)
        return [*accepted, *rejected]

    def _anchor(
        self,
        graph: OrchestraGraph,
        preamble: list[LocalEdit],
        parent_hash: str,
    ) -> LocalCandidate:
        return self._apply_edits(
            graph=graph,
            edits=list(preamble),
            parent_hash=parent_hash,
            candidate_id="cand_feedback",
            reason="failure report only, no design change",
            playbook_id="",
            budget_timeout=None,
        )

    def _from_playbook(
        self,
        playbook: Playbook,
        ctx: PlaybookContext,
        preamble: list[LocalEdit],
        parent_hash: str,
        budget: FastLoopBudget,
    ) -> LocalCandidate:
        candidate_id = f"cand_{playbook.playbook_id}"
        if playbook.layer == "plan":
            return self._recompile(playbook, ctx, parent_hash, candidate_id)
        try:
            extra = bind_edits(playbook, ctx)
        except PlaybookBindError as exc:
            return _rejected(
                candidate_id=candidate_id,
                parent_hash=parent_hash,
                graph=ctx.graph,
                reason=playbook.reason,
                playbook_id=playbook.playbook_id,
                message=str(exc),
            )
        return self._apply_edits(
            graph=ctx.graph,
            edits=[*preamble, *extra],
            parent_hash=parent_hash,
            candidate_id=candidate_id,
            reason=playbook.reason,
            playbook_id=playbook.playbook_id,
            budget_timeout=float(budget.max_wall_time_seconds),
        )

    def _recompile(
        self,
        playbook: Playbook,
        ctx: PlaybookContext,
        parent_hash: str,
        candidate_id: str,
    ) -> LocalCandidate:
        try:
            switch = bind_switch(playbook, ctx)
            return recompile_candidate(
                base_graph=ctx.graph,
                switch=switch,
                candidate_id=candidate_id,
                generation_reason=playbook.reason,
                pool=self.role_pool,
                templates=self.templates,
            )
        except (PlaybookBindError, PlanRecompileError) as exc:
            return _rejected(
                candidate_id=candidate_id,
                parent_hash=parent_hash,
                graph=ctx.graph,
                reason=playbook.reason,
                playbook_id=playbook.playbook_id,
                message=str(exc),
            )

    def _apply_edits(
        self,
        *,
        graph: OrchestraGraph,
        edits: list[LocalEdit],
        parent_hash: str,
        candidate_id: str,
        reason: str,
        playbook_id: str,
        budget_timeout: float | None,
    ) -> LocalCandidate:
        try:
            edited = apply_local_edits(
                graph,
                edits,
                compiler=self.compiler,
                max_timeout_seconds=budget_timeout,
                max_steps_cap=64,
                role_pool=self.role_pool,
            )
        except LocalEditError as exc:
            return _rejected(
                candidate_id=candidate_id,
                parent_hash=parent_hash,
                graph=graph,
                reason=reason,
                playbook_id=playbook_id,
                message=str(exc),
                edits=edits,
            )
        return LocalCandidate(
            candidate_id=candidate_id,
            parent_graph_hash=parent_hash,
            edits=edits,
            graph=edited,
            session_policy=SessionPolicy.FRESH,
            generation_reason=reason,
            playbook_id=playbook_id,
        )


def _rejected(
    *,
    candidate_id: str,
    parent_hash: str,
    graph: OrchestraGraph,
    reason: str,
    playbook_id: str,
    message: str,
    edits: list[LocalEdit] | None = None,
) -> LocalCandidate:
    return LocalCandidate(
        candidate_id=candidate_id,
        parent_graph_hash=parent_hash,
        edits=list(edits or []),
        graph=graph.clone(),
        session_policy=SessionPolicy.FRESH,
        generation_reason=reason,
        playbook_id=playbook_id,
        compatibility_rejected=True,
        rejection_reason=CandidateRejectionReason.INVALID_GRAPH_EDIT,
        rejection_message=message,
    )
