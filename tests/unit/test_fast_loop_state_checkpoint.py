"""FastLoopState checkpoint round-trip and resume semantics."""

from __future__ import annotations

import pytest

from orchestra.backends.capabilities import SessionPolicy
from orchestra.control.fast_loop.schemas import (
    CandidateRecord,
    CandidateStatus,
    FailureDiagnosis,
    FastLoopState,
    PromptFeedbackEdit,
)
from orchestra.control.task_state import (
    SubtaskFailureReason,
    SubtaskStatus,
    TaskExecutionState,
)
from orchestra.decomposition.fallback import build_single_subtask_plan
from orchestra.runtime.task_checkpoint import TaskCheckpointStore

GRAPH = "configs/graphs/codex_single_implementer.yaml"


@pytest.mark.asyncio
async def test_fast_loop_state_roundtrip(tmp_path):
    plan = build_single_subtask_plan(
        task_id="m4_ckpt",
        objective="fix",
        local_graph_template=GRAPH,
        keystone_harness_id="repository_test_harness",
    )
    state = TaskExecutionState.from_plan(plan)
    state.subtasks["main"].status = SubtaskStatus.HARNESS_FAILED
    state.subtasks["main"].failure_reason = SubtaskFailureReason.HARNESS
    fl = FastLoopState(
        subtask_id="main",
        base_attempt_id=1,
        base_graph_hash="abc",
        diagnosis=FailureDiagnosis(
            reason=SubtaskFailureReason.HARNESS,
            retryable=True,
            concise_feedback="fail",
            recommended_edit_types=["prompt_feedback"],
        ),
        candidates=[
            CandidateRecord(
                candidate_id="cand_feedback",
                attempt_id=2,
                graph_hash="g1",
                parent_graph_hash="abc",
                edits=[PromptFeedbackEdit(node_id="n", feedback="fix")],
                status=CandidateStatus.VALID,
                session_policy=SessionPolicy.FRESH,
                quality_score=1.0,
            ),
            CandidateRecord(
                candidate_id="cand_budget",
                attempt_id=2,
                graph_hash="g2",
                parent_graph_hash="abc",
                edits=[],
                status=CandidateStatus.PENDING,
            ),
        ],
    )
    state.fast_loop_states["main"] = fl
    store = TaskCheckpointStore(tmp_path)
    await store.save(state)
    loaded = await store.load("m4_ckpt")
    assert loaded is not None
    restored = loaded.fast_loop_states["main"]
    assert isinstance(restored, FastLoopState)
    assert restored.candidates[0].status is CandidateStatus.VALID
    assert restored.candidates[1].status is CandidateStatus.PENDING
    assert restored.candidates[0].edits[0].type == "prompt_feedback"
