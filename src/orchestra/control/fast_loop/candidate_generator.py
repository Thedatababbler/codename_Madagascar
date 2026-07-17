"""Deterministic rule-based local candidate generation (bounded K)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from orchestra.backends.capabilities import BackendCapabilities, SessionPolicy
from orchestra.control.fast_loop.capability import filter_compatible_candidates
from orchestra.control.fast_loop.edit_engine import LocalEditError, apply_local_edits
from orchestra.control.fast_loop.schemas import (
    BudgetAdjustmentEdit,
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


def _primary_agent_id(graph: OrchestraGraph) -> str | None:
    for node in graph.nodes:
        if node.node_kind is NodeKind.AGENT:
            return node.node_id
    return None


def _alternate_model(graph: OrchestraGraph, node_id: str) -> str | None:
    node = next(n for n in graph.nodes if n.node_id == node_id)
    assert isinstance(node, AgentNodeSpec)
    if node.model is None:
        return None
    name = node.model.name
    # Deterministic tiny alternate mapping for local experiments.
    aliases = {
        "gpt-4o-mini": "gpt-4o",
        "gpt-4o": "gpt-4o-mini",
        "gpt-5.4": "gpt-4o-mini",
    }
    return aliases.get(name)


class RuleBasedLocalCandidateGenerator:
    """Generate at most K=3 FRESH candidates from diagnosis recommendations."""

    def __init__(
        self,
        *,
        compiler: GraphCompiler | None = None,
        alternate_models: Mapping[str, str] | None = None,
        allow_verifier: bool = False,
    ) -> None:
        self.compiler = compiler
        self.alternate_models = dict(alternate_models or {})
        self.allow_verifier = allow_verifier

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

        agent_id = _primary_agent_id(graph)
        if agent_id is None:
            return []

        parent_hash = graph.content_hash
        k = max(0, min(budget.max_candidates, 3))
        drafts: list[tuple[str, str, list[LocalEdit], SessionPolicy]] = []

        recommended = set(diagnosis.recommended_edit_types)

        # Candidate 0: feedback retry (always first when prompt_feedback recommended).
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

        # Candidate 1: bounded budget adjustment.
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

        # Candidate 2: alternate model (preferred) or optional verifier.
        if len(drafts) < k and "model_override" in recommended:
            alt = self.alternate_models.get(agent_id) or _alternate_model(
                graph, agent_id
            )
            if alt:
                drafts.append(
                    (
                        "cand_model",
                        f"alternate model override to {alt}",
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
                        # Applied via edit engine; may raise and be skipped.
                    ],
                    SessionPolicy.FRESH,
                )
            )

        # Ensure at least a fresh feedback candidate when retryable but empty drafts.
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

        candidates: list[LocalCandidate] = []
        for candidate_id, reason, edits, policy in drafts[:k]:
            try:
                final_edits = list(edits)
                if candidate_id == "cand_verifier":
                    from orchestra.control.fast_loop.schemas import AddVerifierNodeEdit

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
            except LocalEditError:
                continue
            candidates.append(
                LocalCandidate(
                    candidate_id=candidate_id,
                    parent_graph_hash=parent_hash,
                    edits=final_edits,
                    graph=edited,
                    session_policy=policy,
                    generation_reason=reason,
                )
            )

        accepted, _rejected = filter_compatible_candidates(candidates, capabilities)
        return accepted
