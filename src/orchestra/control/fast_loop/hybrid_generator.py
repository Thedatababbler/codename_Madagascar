"""Hybrid Codex local candidate generation (RESUME / FORK / FRESH critic)."""

from __future__ import annotations

from collections.abc import Mapping

from orchestra.backends.base import BackendSessionRef
from orchestra.backends.capabilities import BackendCapabilities, SessionPolicy
from orchestra.control.fast_loop.candidate_generator import (
    RuleBasedLocalCandidateGenerator,
    _target_agent_id,
)
from orchestra.control.fast_loop.capability import filter_compatible_candidates
from orchestra.control.fast_loop.edit_engine import LocalEditError, apply_local_edits
from orchestra.control.fast_loop.schemas import (
    AddVerifierNodeEdit,
    CandidateRejectionReason,
    CodexSessionMode,
    FailureDiagnosis,
    FastLoopBudget,
    FastLoopConfig,
    HybridCodexConfig,
    LocalCandidate,
    LocalEdit,
    MissingParentPolicy,
    NodeSessionDirective,
    PromptFeedbackEdit,
    SessionPolicyEdit,
)
from orchestra.control.session_lineage import SessionLineageResolver
from orchestra.control.task_state import TaskExecutionState
from orchestra.ir.compiler import GraphCompilationError, GraphCompiler
from orchestra.ir.graph import OrchestraGraph
from orchestra.ir.nodes import AgentNodeSpec, NodeKind


def _backend_id(graph: OrchestraGraph, node_id: str) -> str:
    node = next(n for n in graph.nodes if n.node_id == node_id)
    assert isinstance(node, AgentNodeSpec)
    return str(node.resolved_backend().type)


def _directive(
    *,
    node_id: str,
    backend_id: str,
    policy: SessionPolicy,
    parent: BackendSessionRef | None,
    reason: str,
    source_node_id: str | None = None,
    source_attempt_id: int | None = None,
) -> NodeSessionDirective:
    require_parent = policy is not SessionPolicy.FRESH
    return NodeSessionDirective(
        node_id=node_id,
        backend_id=backend_id,
        policy=policy,
        source_session_ref=parent if require_parent else None,
        source_node_id=source_node_id or (node_id if require_parent else None),
        source_attempt_id=source_attempt_id,
        workspace_binding="candidate_isolated",
        lineage_reason=reason,
        require_parent_session=require_parent,
    )


class HybridCodexLocalCandidateGenerator:
    """Generate Hybrid Codex candidates or delegate to FRESH-only generator."""

    def __init__(
        self,
        *,
        compiler: GraphCompiler | None = None,
        config: FastLoopConfig | None = None,
        fresh_generator: RuleBasedLocalCandidateGenerator | None = None,
        resolver: SessionLineageResolver | None = None,
    ) -> None:
        self.compiler = compiler
        self.config = config or FastLoopConfig()
        self.fresh_generator = fresh_generator or RuleBasedLocalCandidateGenerator(
            compiler=compiler
        )
        self.resolver = resolver or SessionLineageResolver()

    def generate(
        self,
        *,
        graph: OrchestraGraph,
        diagnosis: FailureDiagnosis,
        budget: FastLoopBudget,
        capabilities: Mapping[str, BackendCapabilities],
        state: TaskExecutionState | None = None,
        subtask_id: str | None = None,
    ) -> list[LocalCandidate]:
        mode = self.config.codex_session_mode
        if mode is CodexSessionMode.FRESH_ONLY or state is None or not subtask_id:
            return self.fresh_generator.generate(
                graph=graph,
                diagnosis=diagnosis,
                budget=budget,
                capabilities=capabilities,
            )
        if diagnosis.infrastructure_related or not diagnosis.retryable:
            return []

        agent_id = _target_agent_id(graph, diagnosis)
        if agent_id is None:
            return []
        backend_id = _backend_id(graph, agent_id)
        if backend_id != "codex_sdk":
            return self.fresh_generator.generate(
                graph=graph,
                diagnosis=diagnosis,
                budget=budget,
                capabilities=capabilities,
            )

        hybrid = self.config.hybrid_codex
        parent = self._resolve_parent(
            state=state,
            subtask_id=subtask_id,
            node_id=agent_id,
            backend_id=backend_id,
        )
        k = max(0, min(budget.max_candidates, 3))
        drafts: list[LocalCandidate] = []

        want_resume = mode in {
            CodexSessionMode.HYBRID,
            CodexSessionMode.RESUME_ONLY,
        } and hybrid.enable_resume
        want_fork = mode in {
            CodexSessionMode.HYBRID,
            CodexSessionMode.FORK_ONLY,
        } and hybrid.enable_fork
        want_critic = (
            mode is CodexSessionMode.HYBRID and hybrid.add_fresh_critic
        )

        if want_resume and len(drafts) < k:
            drafts.extend(
                self._build_session_candidate(
                    graph=graph,
                    diagnosis=diagnosis,
                    budget=budget,
                    agent_id=agent_id,
                    backend_id=backend_id,
                    parent=parent,
                    policy=SessionPolicy.RESUME,
                    candidate_id="cand_resume",
                    reason="same node + same strategy + failure feedback → RESUME",
                    feedback=(
                        f"{diagnosis.concise_feedback}\n"
                        "Continue the same implementation trajectory."
                    ),
                    hybrid=hybrid,
                )
            )

        if want_fork and len(drafts) < k:
            drafts.extend(
                self._build_session_candidate(
                    graph=graph,
                    diagnosis=diagnosis,
                    budget=budget,
                    agent_id=agent_id,
                    backend_id=backend_id,
                    parent=parent,
                    policy=SessionPolicy.FORK,
                    candidate_id="cand_fork",
                    reason="same node + alternative strategy → FORK",
                    feedback=(
                        f"{diagnosis.concise_feedback}\n"
                        "Explore an alternative implementation strategy."
                    ),
                    hybrid=hybrid,
                )
            )

        if want_critic and len(drafts) < k:
            drafts.extend(
                self._build_critic_candidate(
                    graph=graph,
                    diagnosis=diagnosis,
                    budget=budget,
                    agent_id=agent_id,
                    backend_id=backend_id,
                    parent=parent,
                    hybrid=hybrid,
                )
            )

        if not drafts:
            return self.fresh_generator.generate(
                graph=graph,
                diagnosis=diagnosis,
                budget=budget,
                capabilities=capabilities,
            )

        accepted, rejected = filter_compatible_candidates(drafts[:k], capabilities)
        return [*accepted, *rejected]

    def _resolve_parent(
        self,
        *,
        state: TaskExecutionState,
        subtask_id: str,
        node_id: str,
        backend_id: str,
    ) -> BackendSessionRef | None:
        for pref in self.config.hybrid_codex.parent_preference:
            if pref == "failed_initial_attempt":
                parent = self.resolver.resolve_failed_initial_parent(
                    state=state,
                    subtask_id=subtask_id,
                    node_id=node_id,
                    backend_id=backend_id,
                )
                if parent is not None:
                    return parent
            elif pref == "last_selected_candidate":
                parent = self.resolver.resolve_parent(
                    state=state,
                    subtask_id=subtask_id,
                    node_id=node_id,
                    backend_id=backend_id,
                )
                if parent is not None:
                    return parent
        return None

    def _missing_parent_result(
        self,
        *,
        graph: OrchestraGraph,
        diagnosis: FailureDiagnosis,
        agent_id: str,
        backend_id: str,
        candidate_id: str,
        policy: SessionPolicy,
        reason: str,
        hybrid: HybridCodexConfig,
    ) -> list[LocalCandidate]:
        parent_hash = graph.content_hash
        if hybrid.missing_parent_policy is MissingParentPolicy.FRESH_FALLBACK:
            edits: list[LocalEdit] = [
                PromptFeedbackEdit(
                    node_id=agent_id, feedback=diagnosis.concise_feedback
                ),
                SessionPolicyEdit(node_id=agent_id, policy=SessionPolicy.FRESH),
            ]
            try:
                edited = apply_local_edits(
                    graph, edits, compiler=self.compiler, max_steps_cap=64
                )
            except LocalEditError as exc:
                return [
                    LocalCandidate(
                        candidate_id=candidate_id,
                        parent_graph_hash=parent_hash,
                        edits=edits,
                        graph=graph.clone(),
                        session_policy=SessionPolicy.FRESH,
                        session_directives={
                            agent_id: _directive(
                                node_id=agent_id,
                                backend_id=backend_id,
                                policy=SessionPolicy.FRESH,
                                parent=None,
                                reason="fresh_fallback_after_missing_parent",
                            )
                        },
                        generation_reason=f"fresh_fallback: {reason}",
                        compatibility_rejected=True,
                        rejection_reason=CandidateRejectionReason.INVALID_GRAPH_EDIT,
                        rejection_message=str(exc),
                    )
                ]
            return [
                LocalCandidate(
                    candidate_id=f"{candidate_id}_fresh_fallback",
                    parent_graph_hash=parent_hash,
                    edits=edits,
                    graph=edited,
                    session_policy=SessionPolicy.FRESH,
                    session_directives={
                        agent_id: _directive(
                            node_id=agent_id,
                            backend_id=backend_id,
                            policy=SessionPolicy.FRESH,
                            parent=None,
                            reason="fresh_fallback_after_missing_parent",
                        )
                    },
                    generation_reason=(
                        f"parent unavailable for {policy.value}; audited FRESH fallback"
                    ),
                )
            ]
        return [
            LocalCandidate(
                candidate_id=candidate_id,
                parent_graph_hash=parent_hash,
                edits=[],
                graph=graph.clone(),
                session_policy=policy,
                session_directives={},
                generation_reason=reason,
                compatibility_rejected=True,
                rejection_reason=CandidateRejectionReason.UNSUPPORTED_SESSION_POLICY,
                rejection_message=(
                    f"parent session unavailable for {policy.value}; "
                    "missing_parent_policy=reject (no silent FRESH label)"
                ),
            )
        ]

    def _build_session_candidate(
        self,
        *,
        graph: OrchestraGraph,
        diagnosis: FailureDiagnosis,
        budget: FastLoopBudget,
        agent_id: str,
        backend_id: str,
        parent: BackendSessionRef | None,
        policy: SessionPolicy,
        candidate_id: str,
        reason: str,
        feedback: str,
        hybrid: HybridCodexConfig,
    ) -> list[LocalCandidate]:
        if parent is None:
            return self._missing_parent_result(
                graph=graph,
                diagnosis=diagnosis,
                agent_id=agent_id,
                backend_id=backend_id,
                candidate_id=candidate_id,
                policy=policy,
                reason=reason,
                hybrid=hybrid,
            )
        edits: list[LocalEdit] = [
            PromptFeedbackEdit(node_id=agent_id, feedback=feedback),
            SessionPolicyEdit(
                node_id=agent_id,
                policy=policy,
                parent_session_ref=parent,
            ),
        ]
        parent_hash = graph.content_hash
        try:
            edited = apply_local_edits(
                graph,
                edits,
                compiler=self.compiler,
                max_timeout_seconds=float(budget.max_wall_time_seconds),
                max_steps_cap=64,
            )
        except LocalEditError as exc:
            return [
                LocalCandidate(
                    candidate_id=candidate_id,
                    parent_graph_hash=parent_hash,
                    edits=edits,
                    graph=graph.clone(),
                    session_policy=policy,
                    generation_reason=reason,
                    compatibility_rejected=True,
                    rejection_reason=CandidateRejectionReason.INVALID_GRAPH_EDIT,
                    rejection_message=str(exc),
                )
            ]
        directive = _directive(
            node_id=agent_id,
            backend_id=backend_id,
            policy=policy,
            parent=parent,
            reason=reason,
        )
        # Mirror policy onto the edited agent node for audit; execution uses directives.
        nodes = []
        for node in edited.nodes:
            if node.node_id == agent_id and isinstance(node, AgentNodeSpec):
                nodes.append(
                    node.model_copy(update={"session_policy": policy.value})
                )
            else:
                nodes.append(node)
        edited = edited.model_copy(update={"nodes": nodes})
        return [
            LocalCandidate(
                candidate_id=candidate_id,
                parent_graph_hash=parent_hash,
                edits=edits,
                graph=edited,
                session_policy=policy,
                session_directives={agent_id: directive},
                generation_reason=reason,
            )
        ]

    def _build_critic_candidate(
        self,
        *,
        graph: OrchestraGraph,
        diagnosis: FailureDiagnosis,
        budget: FastLoopBudget,
        agent_id: str,
        backend_id: str,
        parent: BackendSessionRef | None,
        hybrid: HybridCodexConfig,
    ) -> list[LocalCandidate]:
        """Fresh critic verifier + FORK/RESUME repairer on the implementer node."""
        if parent is None and hybrid.missing_parent_policy is MissingParentPolicy.REJECT:
            return self._missing_parent_result(
                graph=graph,
                diagnosis=diagnosis,
                agent_id=agent_id,
                backend_id=backend_id,
                candidate_id="cand_critic_repair",
                policy=SessionPolicy.FORK,
                reason="fresh critic + forked repairer (parent missing)",
                hybrid=hybrid,
            )
        repair_policy = (
            SessionPolicy.FORK if hybrid.enable_fork else SessionPolicy.RESUME
        )
        if parent is None:
            return self._missing_parent_result(
                graph=graph,
                diagnosis=diagnosis,
                agent_id=agent_id,
                backend_id=backend_id,
                candidate_id="cand_critic_repair",
                policy=repair_policy,
                reason="fresh critic + repair",
                hybrid=hybrid,
            )
        edits: list[LocalEdit] = [
            PromptFeedbackEdit(
                node_id=agent_id,
                feedback=(
                    f"{diagnosis.concise_feedback}\n"
                    "Apply the independent critic findings and repair the repository."
                ),
            ),
            AddVerifierNodeEdit(
                target_node_id=agent_id,
                verifier_template_id="default_structured_verifier",
            ),
            SessionPolicyEdit(
                node_id=agent_id,
                policy=repair_policy,
                parent_session_ref=parent,
            ),
        ]
        parent_hash = graph.content_hash
        try:
            edited = apply_local_edits(
                graph,
                edits,
                compiler=self.compiler,
                max_timeout_seconds=float(budget.max_wall_time_seconds),
                max_steps_cap=64,
            )
        except (LocalEditError, GraphCompilationError):
            # Graph may not support verifier insertion; still emit repair FORK.
            return self._build_session_candidate(
                graph=graph,
                diagnosis=diagnosis,
                budget=budget,
                agent_id=agent_id,
                backend_id=backend_id,
                parent=parent,
                policy=repair_policy,
                candidate_id="cand_critic_repair",
                reason="fresh critic unavailable; forked/resumed repairer only",
                feedback=(
                    f"{diagnosis.concise_feedback}\n"
                    "Independent critique unavailable; repair via session branch."
                ),
                hybrid=hybrid,
            )

        directives: dict[str, NodeSessionDirective] = {
            agent_id: _directive(
                node_id=agent_id,
                backend_id=backend_id,
                policy=repair_policy,
                parent=parent,
                reason="repair implementer FORK/RESUME from original session",
            )
        }
        for node in edited.nodes:
            if node.node_kind is not NodeKind.AGENT:
                continue
            assert isinstance(node, AgentNodeSpec)
            if node.node_id.endswith("__verifier"):
                directives[node.node_id] = _directive(
                    node_id=node.node_id,
                    backend_id=str(node.resolved_backend().type),
                    policy=SessionPolicy.FRESH,
                    parent=None,
                    reason="new critic/verifier node → FRESH",
                )
        return [
            LocalCandidate(
                candidate_id="cand_critic_repair",
                parent_graph_hash=parent_hash,
                edits=edits,
                graph=edited,
                session_policy=repair_policy,
                session_directives=directives,
                generation_reason=(
                    "fresh independent critic → forked/resumed repairer → harness"
                ),
            )
        ]
