"""Unit tests for Fast Loop failure diagnosis."""

from __future__ import annotations

from orchestra.control.fast_loop.diagnosis import diagnose_subtask_failure
from orchestra.control.task_state import (
    SubtaskFailureReason,
    SubtaskState,
    SubtaskStatus,
)
from orchestra.decomposition.schemas import BudgetSpec, SubtaskSpec
from orchestra.ir.graph import load_graph

GRAPH = "configs/graphs/codex_single_implementer.yaml"


def _sub(status: SubtaskStatus, reason: SubtaskFailureReason | None, msg: str) -> SubtaskState:
    return SubtaskState(
        spec=SubtaskSpec(
            subtask_id="s1",
            title="t",
            objective="o",
            dependencies=[],
            keystone_harness_id="repository_test_harness",
            local_graph_template=GRAPH,
            budget=BudgetSpec(max_llm_calls=1, max_steps=1, timeout_seconds=60),
        ),
        status=status,
        failure_reason=reason,
        failure_message=msg,
    )


def test_harness_failure_diagnosis():
    d = diagnose_subtask_failure(
        subtask_state=_sub(
            SubtaskStatus.HARNESS_FAILED,
            SubtaskFailureReason.HARNESS,
            "pytest failed",
        ),
        graph=load_graph(GRAPH),
    )
    assert d.retryable is True
    assert d.infrastructure_related is False
    assert "prompt_feedback" in d.recommended_edit_types
    assert "budget_adjustment" in d.recommended_edit_types


def test_infra_failure_no_graph_edits():
    d = diagnose_subtask_failure(
        subtask_state=_sub(
            SubtaskStatus.FAILED,
            SubtaskFailureReason.INFRA,
            "disk full",
        ),
        graph=load_graph(GRAPH),
    )
    assert d.infrastructure_related is True
    assert d.recommended_edit_types == []


def test_invalid_config_not_retryable():
    d = diagnose_subtask_failure(
        subtask_state=_sub(
            SubtaskStatus.FAILED,
            SubtaskFailureReason.INVALID_CONFIG,
            "bad yaml",
        ),
        graph=load_graph(GRAPH),
    )
    assert d.retryable is False
