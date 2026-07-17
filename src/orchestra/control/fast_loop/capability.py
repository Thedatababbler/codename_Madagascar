"""Capability negotiation for Fast Loop candidates (no silent downgrade)."""

from __future__ import annotations

from collections.abc import Mapping

from orchestra.backends.capabilities import BackendCapabilities, SessionPolicy
from orchestra.control.fast_loop.schemas import (
    BudgetAdjustmentEdit,
    CandidateCompatibilityResult,
    LocalCandidate,
    LocalEdit,
    ModelOverrideEdit,
    SessionPolicyEdit,
    ToolPolicyEdit,
)
from orchestra.ir.nodes import AgentNodeSpec, NodeKind


def backend_ids_for_candidate(candidate: LocalCandidate) -> set[str]:
    ids: set[str] = set()
    for node in candidate.graph.nodes:
        if node.node_kind is not NodeKind.AGENT:
            continue
        assert isinstance(node, AgentNodeSpec)
        ids.add(str(node.resolved_backend().type))
    return ids


def validate_edit_against_capabilities(
    edit: LocalEdit,
    capabilities: BackendCapabilities,
) -> CandidateCompatibilityResult:
    if isinstance(edit, SessionPolicyEdit):
        if not capabilities.supports_policy(edit.policy):
            return CandidateCompatibilityResult(
                compatible=False,
                reason=(
                    f"session policy {edit.policy.value!r} not in "
                    f"supported_session_policies="
                    f"{sorted(p.value for p in capabilities.supported_session_policies)}"
                ),
            )
        if edit.policy is not SessionPolicy.FRESH and not capabilities.supports_session_state:
            return CandidateCompatibilityResult(
                compatible=False,
                reason="backend does not support session state for resume/fork",
            )
        return CandidateCompatibilityResult(compatible=True)

    if isinstance(edit, ToolPolicyEdit):
        if not capabilities.supports_tool_policy_edit:
            return CandidateCompatibilityResult(
                compatible=False,
                reason="backend does not support tool_policy edits",
            )
        return CandidateCompatibilityResult(compatible=True)

    if isinstance(edit, ModelOverrideEdit):
        if not capabilities.supports_model_override:
            return CandidateCompatibilityResult(
                compatible=False,
                reason="backend does not support model_override edits",
            )
        return CandidateCompatibilityResult(compatible=True)

    if isinstance(edit, BudgetAdjustmentEdit):
        return CandidateCompatibilityResult(compatible=True)

    return CandidateCompatibilityResult(compatible=True)


def validate_candidate_against_capabilities(
    candidate: LocalCandidate,
    capabilities: BackendCapabilities | Mapping[str, BackendCapabilities],
) -> CandidateCompatibilityResult:
    """Reject unsupported candidates before execution; never silent-downgrade."""
    if isinstance(capabilities, BackendCapabilities):
        caps_by_backend = {"*": capabilities}
        single = capabilities
    else:
        caps_by_backend = dict(capabilities)
        single = None

    # Candidate-level session policy must be supported by every agent backend.
    backend_ids = backend_ids_for_candidate(candidate)
    if not backend_ids and single is None:
        return CandidateCompatibilityResult(
            compatible=False,
            reason="candidate has no agent backends to negotiate capabilities",
        )

    for backend_id in sorted(backend_ids):
        caps = single or caps_by_backend.get(backend_id)
        if caps is None:
            return CandidateCompatibilityResult(
                compatible=False,
                reason=f"unknown backend capabilities for {backend_id!r}",
            )
        if not caps.supports_policy(candidate.session_policy):
            return CandidateCompatibilityResult(
                compatible=False,
                reason=(
                    f"backend {backend_id!r} does not support session policy "
                    f"{candidate.session_policy.value!r}; no silent downgrade"
                ),
            )
        if (
            candidate.session_policy is not SessionPolicy.FRESH
            and not caps.supports_workspace_rebinding
        ):
            return CandidateCompatibilityResult(
                compatible=False,
                reason=(
                    f"backend {backend_id!r} cannot rebind workspace for "
                    f"{candidate.session_policy.value!r}"
                ),
            )
        for edit in candidate.edits:
            result = validate_edit_against_capabilities(edit, caps)
            if not result.compatible:
                return CandidateCompatibilityResult(
                    compatible=False,
                    reason=f"backend {backend_id!r}: {result.reason}",
                )
            # Per-node session overlays on the graph.
            if isinstance(edit, SessionPolicyEdit) and not caps.supports_policy(
                edit.policy
            ):
                return CandidateCompatibilityResult(
                    compatible=False,
                    reason=(
                        f"backend {backend_id!r} rejects session_policy edit "
                        f"{edit.policy.value!r}"
                    ),
                )

    for node in candidate.graph.nodes:
        if node.node_kind is not NodeKind.AGENT:
            continue
        assert isinstance(node, AgentNodeSpec)
        if not node.session_policy:
            continue
        policy = SessionPolicy(node.session_policy)
        backend_id = str(node.resolved_backend().type)
        caps = single or caps_by_backend.get(backend_id)
        if caps is None:
            return CandidateCompatibilityResult(
                compatible=False,
                reason=f"unknown backend capabilities for {backend_id!r}",
            )
        if not caps.supports_policy(policy):
            return CandidateCompatibilityResult(
                compatible=False,
                reason=(
                    f"node {node.node_id} session_policy={policy.value!r} "
                    f"unsupported by {backend_id!r}; no silent downgrade"
                ),
            )

    return CandidateCompatibilityResult(compatible=True)


def filter_compatible_candidates(
    candidates: list[LocalCandidate],
    capabilities: Mapping[str, BackendCapabilities],
) -> tuple[list[LocalCandidate], list[LocalCandidate]]:
    """Return (accepted, rejected). Rejected candidates keep rejection_reason."""
    accepted: list[LocalCandidate] = []
    rejected: list[LocalCandidate] = []
    for candidate in candidates:
        result = validate_candidate_against_capabilities(candidate, capabilities)
        if result.compatible:
            accepted.append(candidate)
        else:
            rejected.append(
                candidate.model_copy(
                    update={
                        "compatibility_rejected": True,
                        "rejection_reason": result.reason,
                    }
                )
            )
    return accepted, rejected
