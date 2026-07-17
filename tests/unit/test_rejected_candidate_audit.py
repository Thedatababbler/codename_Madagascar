"""Capability-rejected candidates must persist as REJECTED with zero backend cost."""

from __future__ import annotations

import pytest

from orchestra.backends.capabilities import BackendCapabilities, SessionPolicy
from orchestra.control.fast_loop.capability import validate_candidate_against_capabilities
from orchestra.control.fast_loop.edit_engine import apply_local_edits
from orchestra.control.fast_loop.schemas import (
    CandidateRecord,
    CandidateRejectionReason,
    CandidateStatus,
    FailureDiagnosis,
    FastLoopState,
    LocalCandidate,
    SessionPolicyEdit,
)
from orchestra.control.task_state import SubtaskFailureReason, TaskExecutionState
from orchestra.decomposition.fallback import build_single_subtask_plan
from orchestra.ir.graph import load_graph
from orchestra.runtime.task_checkpoint import TaskCheckpointStore

GRAPH = "configs/graphs/codex_single_implementer.yaml"


@pytest.mark.asyncio
async def test_codeagent_fork_rejected_before_execution_and_checkpointed(tmp_path):
    base = load_graph(GRAPH)
    edits = [
        SessionPolicyEdit(node_id="codex_implementer", policy=SessionPolicy.FORK),
    ]
    graph = apply_local_edits(base, edits)
    cand = LocalCandidate(
        candidate_id="cand_fork",
        parent_graph_hash=base.content_hash,
        edits=edits,
        graph=graph,
        session_policy=SessionPolicy.FORK,
        generation_reason="audit fork reject",
    )
    caps = BackendCapabilities(
        multi_step=True,
        supports_session_state=False,
        supported_session_policies=frozenset({SessionPolicy.FRESH}),
        supports_parallel_instances=True,
    )
    result = validate_candidate_against_capabilities(cand, caps)
    assert result.compatible is False
    assert result.rejection_reason is CandidateRejectionReason.UNSUPPORTED_SESSION_POLICY

    plan = build_single_subtask_plan(
        task_id="reject_audit",
        objective="x",
        local_graph_template=GRAPH,
        keystone_harness_id="repository_test_harness",
    )
    state = TaskExecutionState.from_plan(plan)
    fl = FastLoopState(
        subtask_id="main",
        base_attempt_id=1,
        base_graph_hash=base.content_hash,
        diagnosis=FailureDiagnosis(
            reason=SubtaskFailureReason.HARNESS,
            retryable=True,
            concise_feedback="x",
            primary_failed_node_id="codex_implementer",
            failed_node_ids=["codex_implementer"],
        ),
        candidates=[
            CandidateRecord(
                candidate_id="cand_fork",
                attempt_id=2,
                graph_hash=graph.content_hash,
                parent_graph_hash=base.content_hash,
                edits=edits,
                status=CandidateStatus.REJECTED,
                session_policy=SessionPolicy.FORK,
                rejection_reason=result.rejection_reason,
                rejection_message=result.reason,
            )
        ],
    )
    state.fast_loop_states["main"] = fl
    store = TaskCheckpointStore(tmp_path)
    await store.save(state)
    loaded = await store.load("reject_audit")
    assert loaded is not None
    restored = loaded.fast_loop_states["main"].candidates[0]
    assert restored.status is CandidateStatus.REJECTED
    assert restored.rejection_reason is CandidateRejectionReason.UNSUPPORTED_SESSION_POLICY
    assert restored.cost.backend_calls == 0
