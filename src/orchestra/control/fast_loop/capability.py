"""Capability negotiation for Fast Loop candidates (no silent downgrade)."""

from __future__ import annotations

from collections.abc import Mapping

from orchestra.backends.capabilities import BackendCapabilities, SessionPolicy
from orchestra.control.fast_loop.schemas import (
    BudgetAdjustmentEdit,
    CandidateCompatibilityResult,
    CandidateRejectionReason,
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


def _reject(
    reason: str,
    code: CandidateRejectionReason,
) -> CandidateCompatibilityResult:
    return CandidateCompatibilityResult(
        compatible=False,
        reason=reason,
        rejection_reason=code,
    )


def validate_edit_against_capabilities(
    edit: LocalEdit,
    capabilities: BackendCapabilities,
) -> CandidateCompatibilityResult:
    if isinstance(edit, SessionPolicyEdit):
        if not capabilities.supports_policy(edit.policy):
            return _reject(
                (
                    f"session policy {edit.policy.value!r} not in "
                    f"supported_session_policies="
                    f"{sorted(p.value for p in capabilities.supported_session_policies)}"
                ),
                CandidateRejectionReason.UNSUPPORTED_SESSION_POLICY,
            )
        if edit.policy is not SessionPolicy.FRESH and not capabilities.supports_session_state:
            return _reject(
                "backend does not support session state for resume/fork",
                CandidateRejectionReason.UNSUPPORTED_SESSION_POLICY,
            )
        return CandidateCompatibilityResult(compatible=True)

    if isinstance(edit, ToolPolicyEdit):
        if not capabilities.supports_tool_policy_edit:
            return _reject(
                "backend does not support tool_policy edits",
                CandidateRejectionReason.UNSUPPORTED_TOOL_EDIT,
            )
        return CandidateCompatibilityResult(compatible=True)

    if isinstance(edit, ModelOverrideEdit):
        if not capabilities.supports_model_override:
            return _reject(
                "backend does not support model_override edits",
                CandidateRejectionReason.UNSUPPORTED_MODEL_OVERRIDE,
            )
        return CandidateCompatibilityResult(compatible=True)

    if isinstance(edit, BudgetAdjustmentEdit):
        return CandidateCompatibilityResult(compatible=True)

    return CandidateCompatibilityResult(compatible=True)


def validate_candidate_against_capabilities(
    candidate: LocalCandidate,
    capabilities: BackendCapabilities | Mapping[str, BackendCapabilities],
) -> CandidateCompatibilityResult:
    """Reject unsupported candidates before execution; never silent-downgrade.

    When ``session_directives`` are present, validate each node directive against
    that node's backend only (mixed policies allowed).
    """
    if isinstance(capabilities, BackendCapabilities):
        caps_by_backend = {"*": capabilities}
        single = capabilities
    else:
        caps_by_backend = dict(capabilities)
        single = None

    backend_ids = backend_ids_for_candidate(candidate)
    if not backend_ids and single is None:
        return _reject(
            "candidate has no agent backends to negotiate capabilities",
            CandidateRejectionReason.OTHER,
        )

    directives = dict(candidate.session_directives or {})

    if directives:
        for node_id, directive in sorted(directives.items()):
            caps = single or caps_by_backend.get(directive.backend_id)
            if caps is None:
                return _reject(
                    f"unknown backend capabilities for {directive.backend_id!r}",
                    CandidateRejectionReason.OTHER,
                )
            if not caps.supports_policy(directive.policy):
                return _reject(
                    (
                        f"node {node_id} policy={directive.policy.value!r} unsupported "
                        f"by {directive.backend_id!r}; no silent downgrade"
                    ),
                    CandidateRejectionReason.UNSUPPORTED_SESSION_POLICY,
                )
            if directive.policy is SessionPolicy.RESUME and not caps.supports_resume:
                return _reject(
                    f"backend {directive.backend_id!r} supports_resume=false",
                    CandidateRejectionReason.UNSUPPORTED_SESSION_POLICY,
                )
            if directive.policy is SessionPolicy.FORK and not caps.supports_fork:
                return _reject(
                    f"backend {directive.backend_id!r} supports_fork=false",
                    CandidateRejectionReason.UNSUPPORTED_SESSION_POLICY,
                )
            if directive.policy is not SessionPolicy.FRESH:
                if not caps.supports_workspace_rebinding:
                    return _reject(
                        (
                            f"backend {directive.backend_id!r} cannot rebind workspace "
                            f"for {directive.policy.value!r}"
                        ),
                        CandidateRejectionReason.WORKSPACE_INCOMPATIBLE,
                    )
                if (
                    directive.policy is SessionPolicy.RESUME
                    and not caps.supports_cross_workspace_resume
                ):
                    return _reject(
                        f"backend {directive.backend_id!r} lacks cross_workspace_resume",
                        CandidateRejectionReason.WORKSPACE_INCOMPATIBLE,
                    )
                if (
                    directive.policy is SessionPolicy.FORK
                    and not caps.supports_cross_workspace_fork
                ):
                    return _reject(
                        f"backend {directive.backend_id!r} lacks cross_workspace_fork",
                        CandidateRejectionReason.WORKSPACE_INCOMPATIBLE,
                    )
                if directive.require_parent_session and directive.source_session_ref is None:
                    return _reject(
                        f"node {node_id} {directive.policy.value} missing parent session",
                        CandidateRejectionReason.UNSUPPORTED_SESSION_POLICY,
                    )
            # Validate edits only against the backend they target when possible.
            for edit in candidate.edits:
                target = getattr(edit, "node_id", None) or getattr(
                    edit, "target_node_id", None
                )
                if target is not None and target != node_id:
                    continue
                result = validate_edit_against_capabilities(edit, caps)
                if not result.compatible:
                    return CandidateCompatibilityResult(
                        compatible=False,
                        reason=f"backend {directive.backend_id!r}: {result.reason}",
                        rejection_reason=result.rejection_reason
                        or CandidateRejectionReason.OTHER,
                    )
        return CandidateCompatibilityResult(compatible=True)

    # Legacy path: candidate-level session_policy applied per backend.
    for backend_id in sorted(backend_ids):
        caps = single or caps_by_backend.get(backend_id)
        if caps is None:
            return _reject(
                f"unknown backend capabilities for {backend_id!r}",
                CandidateRejectionReason.OTHER,
            )
        if not caps.supports_policy(candidate.session_policy):
            return _reject(
                (
                    f"backend {backend_id!r} does not support session policy "
                    f"{candidate.session_policy.value!r}; no silent downgrade"
                ),
                CandidateRejectionReason.UNSUPPORTED_SESSION_POLICY,
            )
        if (
            candidate.session_policy is not SessionPolicy.FRESH
            and not caps.supports_workspace_rebinding
        ):
            return _reject(
                (
                    f"backend {backend_id!r} cannot rebind workspace for "
                    f"{candidate.session_policy.value!r}"
                ),
                CandidateRejectionReason.WORKSPACE_INCOMPATIBLE,
            )
        for edit in candidate.edits:
            result = validate_edit_against_capabilities(edit, caps)
            if not result.compatible:
                return CandidateCompatibilityResult(
                    compatible=False,
                    reason=f"backend {backend_id!r}: {result.reason}",
                    rejection_reason=result.rejection_reason
                    or CandidateRejectionReason.OTHER,
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
            return _reject(
                f"unknown backend capabilities for {backend_id!r}",
                CandidateRejectionReason.OTHER,
            )
        if not caps.supports_policy(policy):
            return _reject(
                (
                    f"node {node.node_id} session_policy={policy.value!r} "
                    f"unsupported by {backend_id!r}; no silent downgrade"
                ),
                CandidateRejectionReason.UNSUPPORTED_SESSION_POLICY,
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
                        "rejection_reason": result.rejection_reason
                        or CandidateRejectionReason.OTHER,
                        "rejection_message": result.reason,
                    }
                )
            )
    return accepted, rejected
