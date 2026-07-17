"""Capability negotiation: no silent session-policy downgrade."""

from __future__ import annotations

from orchestra.backends.capabilities import BackendCapabilities, SessionPolicy
from orchestra.backends.catalog import capabilities_for
from orchestra.control.fast_loop.capability import (
    filter_compatible_candidates,
    validate_candidate_against_capabilities,
)
from orchestra.control.fast_loop.edit_engine import apply_local_edits
from orchestra.control.fast_loop.schemas import LocalCandidate, SessionPolicyEdit
from orchestra.ir.graph import load_graph

GRAPH = "configs/graphs/codex_single_implementer.yaml"


def _candidate(policy: SessionPolicy) -> LocalCandidate:
    base = load_graph(GRAPH)
    edits = [
        SessionPolicyEdit(node_id="codex_implementer", policy=policy),
    ]
    # apply_local_edits always writes session_policy on the node.
    graph = apply_local_edits(base, edits)
    return LocalCandidate(
        candidate_id=f"c_{policy.value}",
        parent_graph_hash=base.content_hash,
        edits=edits,
        graph=graph,
        session_policy=policy,
        generation_reason="test",
    )


def test_codeagent_rejects_resume_and_fork():
    caps = {"smolagents_code": capabilities_for("smolagents_code")}
    # Use a graph with smolagents would be better; negotiate against CodeAgent caps.
    for policy in (SessionPolicy.RESUME, SessionPolicy.FORK):
        cand = _candidate(policy)
        # Force capability map as if agents were CodeAgent.
        result = validate_candidate_against_capabilities(
            cand,
            caps["smolagents_code"],
        )
        assert result.compatible is False
        assert "no silent downgrade" in (result.reason or "")


def test_codex_fresh_only_rejects_fork():
    caps = capabilities_for("codex_sdk")
    assert caps is not None
    assert SessionPolicy.FORK not in caps.supported_session_policies
    result = validate_candidate_against_capabilities(_candidate(SessionPolicy.FORK), caps)
    assert result.compatible is False


def test_fresh_accepted_for_both():
    for backend_id in ("smolagents_code", "codex_sdk"):
        caps = capabilities_for(backend_id)
        assert caps is not None
        result = validate_candidate_against_capabilities(
            _candidate(SessionPolicy.FRESH), caps
        )
        assert result.compatible is True


def test_filter_keeps_rejection_reason():
    caps = {
        "codex_sdk": BackendCapabilities(
            multi_step=True,
            repository_editing=True,
            supports_session_state=True,
            supported_session_policies=frozenset({SessionPolicy.FRESH}),
            supports_parallel_instances=True,
        )
    }
    accepted, rejected = filter_compatible_candidates(
        [_candidate(SessionPolicy.RESUME)],
        caps,
    )
    assert accepted == []
    assert rejected[0].compatibility_rejected is True
    assert rejected[0].rejection_reason is not None
    assert rejected[0].rejection_message
