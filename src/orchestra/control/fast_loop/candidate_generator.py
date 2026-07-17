"""Deterministic rule-based local candidate generation (bounded K)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from orchestra.backends.capabilities import BackendCapabilities, SessionPolicy
from orchestra.control.fast_loop.capability import filter_compatible_candidates
from orchestra.control.fast_loop.edit_engine import LocalEditError, apply_local_edits
from orchestra.control.fast_loop.schemas import (
    AddVerifierNodeEdit,
    BackendModelPool,
    BudgetAdjustmentEdit,
    CandidateRejectionReason,
    FailureDiagnosis,
    FastLoopBudget,
    LocalCandidate,
    LocalEdit,
    ModelOverrideEdit,
    PromptFeedbackEdit,
    SessionPolicyEdit,
)
from orchestra.ir.compiler import GraphCompiler
from orchestra.ir.graph import OrchestraGraph
from orchestra.ir.nodes import AgentNodeSpec, NodeKind


class LocalCandidateGenerator(Protocol):
    def generate(
        self,
        *,
        graph: OrchestraGraph,
        diagnosis: FailureDiagnosis,
        budget: FastLoopBudget,
        capabilities: Mapping[str, BackendCapabilities],
    ) -> list[LocalCandidate]: ...


def _target_agent_id(graph: OrchestraGraph, diagnosis: FailureDiagnosis) -> str | None:
    """Edit the primary failed agent node; never silently pick the first agent."""
    if diagnosis.primary_failed_node_id:
        node = next(
            (n for n in graph.nodes if n.node_id == diagnosis.primary_failed_node_id),
            None,
        )
        if node is not None and node.node_kind is NodeKind.AGENT:
            return node.node_id
        # Harness primary: walk failed_node_ids for an agent.
    for node_id in diagnosis.failed_node_ids:
        node = next((n for n in graph.nodes if n.node_id == node_id), None)
        if node is not None and node.node_kind is NodeKind.AGENT:
            return node.node_id
    return None


def _alternate_from_pool(
    graph: OrchestraGraph,
    node_id: str,
    pools: Mapping[str, BackendModelPool],
) -> str | None:
    node = next(n for n in graph.nodes if n.node_id == node_id)
    assert isinstance(node, AgentNodeSpec)
    backend_id = str(node.resolved_backend().type)
    pool = pools.get(backend_id)
    if pool is None or not pool.allowed_models:
        return None
    current = node.model.name if node.model else None
    order = list(pool.fallback_order) or list(pool.allowed_models)
    for name in order:
        if name != current and name in pool.allowed_models:
            return name
    for name in pool.allowed_models:
        if name != current:
            return name
    return None


class RuleBasedLocalCandidateGenerator:
    """Generate at most K=3 FRESH candidates from diagnosis recommendations."""

    def __init__(
        self,
        *,
        compiler: GraphCompiler | None = None,
        model_pools: Mapping[str, BackendModelPool] | None = None,
        allow_verifier: bool = False,
    ) -> None:
        self.compiler = compiler
        self.model_pools = dict(model_pools or {})
        self.allow_verifier = allow_verifier

    def generate(
        self,
        *,
        graph: OrchestraGraph,
        diagnosis: FailureDiagnosis,
        budget: FastLoopBudget,
        capabilities: Mapping[str, BackendCapabilities],
    ) -> list[LocalCandidate]:
        """Return accepted + capability-rejected candidates (all audited)."""
        if diagnosis.infrastructure_related or not diagnosis.retryable:
            return []

        agent_id = _target_agent_id(graph, diagnosis)
        if agent_id is None:
            # No node-specific edits without a clear failed agent.
            return []

        parent_hash = graph.content_hash
        k = max(0, min(budget.max_candidates, 3))
        drafts: list[tuple[str, str, list[LocalEdit], SessionPolicy]] = []
        recommended = set(diagnosis.recommended_edit_types)

        if "prompt_feedback" in recommended or "fresh_retry" in recommended:
            drafts.append(
                (
                    "cand_feedback",
                    "attach harness/backend failure feedback and retry fresh",
                    [
                        PromptFeedbackEdit(
                            node_id=agent_id,
                            feedback=diagnosis.concise_feedback,
                        ),
                        SessionPolicyEdit(
                            node_id=agent_id,
                            policy=SessionPolicy.FRESH,
                        ),
                    ],
                    SessionPolicy.FRESH,
                )
            )

        if "budget_adjustment" in recommended and len(drafts) < k:
            drafts.append(
                (
                    "cand_budget",
                    "small max_steps/timeout increase within remaining budget",
                    [
                        BudgetAdjustmentEdit(
                            node_id=agent_id,
                            max_steps_delta=2,
                            timeout_seconds_delta=30,
                        ),
                        PromptFeedbackEdit(
                            node_id=agent_id,
                            feedback=diagnosis.concise_feedback,
                        ),
                        SessionPolicyEdit(
                            node_id=agent_id,
                            policy=SessionPolicy.FRESH,
                        ),
                    ],
                    SessionPolicy.FRESH,
                )
            )

        if len(drafts) < k and "model_override" in recommended:
            alt = _alternate_from_pool(graph, agent_id, self.model_pools)
            if alt:
                drafts.append(
                    (
                        "cand_model",
                        f"alternate model override to {alt} from configured pool",
                        [
                            ModelOverrideEdit(node_id=agent_id, model_name=alt),
                            PromptFeedbackEdit(
                                node_id=agent_id,
                                feedback=diagnosis.concise_feedback,
                            ),
                            SessionPolicyEdit(
                                node_id=agent_id,
                                policy=SessionPolicy.FRESH,
                            ),
                        ],
                        SessionPolicy.FRESH,
                    )
                )
        elif (
            len(drafts) < k
            and self.allow_verifier
            and "add_verifier_node" in recommended
        ):
            drafts.append(
                (
                    "cand_verifier",
                    "insert default structured verifier after coder",
                    [
                        PromptFeedbackEdit(
                            node_id=agent_id,
                            feedback=diagnosis.concise_feedback,
                        ),
                    ],
                    SessionPolicy.FRESH,
                )
            )

        if not drafts and diagnosis.retryable:
            drafts.append(
                (
                    "cand_feedback",
                    "default fresh feedback retry",
                    [
                        PromptFeedbackEdit(
                            node_id=agent_id,
                            feedback=diagnosis.concise_feedback,
                        ),
                        SessionPolicyEdit(
                            node_id=agent_id,
                            policy=SessionPolicy.FRESH,
                        ),
                    ],
                    SessionPolicy.FRESH,
                )
            )

        built: list[LocalCandidate] = []
        for candidate_id, reason, edits, policy in drafts[:k]:
            try:
                final_edits = list(edits)
                if candidate_id == "cand_verifier":
                    final_edits.append(
                        AddVerifierNodeEdit(
                            target_node_id=agent_id,
                            verifier_template_id="default_structured_verifier",
                        )
                    )
                edited = apply_local_edits(
                    graph,
                    final_edits,
                    compiler=self.compiler,
                    max_timeout_seconds=float(budget.max_wall_time_seconds),
                    max_steps_cap=64,
                )
            except LocalEditError as exc:
                built.append(
                    LocalCandidate(
                        candidate_id=candidate_id,
                        parent_graph_hash=parent_hash,
                        edits=list(edits),
                        graph=graph.clone(),
                        session_policy=policy,
                        generation_reason=reason,
                        compatibility_rejected=True,
                        rejection_reason=CandidateRejectionReason.INVALID_GRAPH_EDIT,
                        rejection_message=str(exc),
                    )
                )
                continue
            built.append(
                LocalCandidate(
                    candidate_id=candidate_id,
                    parent_graph_hash=parent_hash,
                    edits=final_edits,
                    graph=edited,
                    session_policy=policy,
                    generation_reason=reason,
                )
            )

        accepted, rejected = filter_compatible_candidates(built, capabilities)
        # Preserve generation order: accepted first then rejected for audit.
        return [*accepted, *rejected]
