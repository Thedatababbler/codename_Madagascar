"""Failure diagnosis for Fast Loop candidate generation."""

from __future__ import annotations

from orchestra.control.fast_loop.schemas import FailureDiagnosis
from orchestra.control.task_state import SubtaskFailureReason, SubtaskState, SubtaskStatus
from orchestra.ir.graph import OrchestraGraph
from orchestra.ir.nodes import NodeKind
from orchestra.runtime.state import NodeExecutionResult


def diagnose_subtask_failure(
    *,
    subtask_state: SubtaskState,
    graph: OrchestraGraph,
    node_results: dict[str, NodeExecutionResult] | None = None,
) -> FailureDiagnosis:
    """Map subtask/backend/harness outcomes to a structured FailureDiagnosis."""
    reason = subtask_state.failure_reason
    if reason is None:
        if subtask_state.status is SubtaskStatus.HARNESS_FAILED:
            reason = SubtaskFailureReason.HARNESS
        else:
            reason = SubtaskFailureReason.UNKNOWN
    message = (subtask_state.failure_message or "").strip()
    evidence: list[str] = [
        a.artifact_id for a in subtask_state.committed_artifacts
    ]
    if subtask_state.final_output_artifact_id:
        evidence.append(subtask_state.final_output_artifact_id)
    for attempt in subtask_state.attempts:
        for key in ("output_artifact_ids", "evidence_artifact_ids"):
            raw = attempt.metadata.get(key)
            if isinstance(raw, list):
                evidence.extend(str(x) for x in raw)

    if reason is SubtaskFailureReason.HARNESS:
        return FailureDiagnosis(
            reason=reason,
            retryable=True,
            evidence_artifact_ids=_uniq(evidence),
            concise_feedback=_truncate(message or "Harness verification failed."),
            recommended_edit_types=[
                "prompt_feedback",
                "budget_adjustment",
                "add_verifier_node",
                "model_override",
            ],
            infrastructure_related=False,
        )

    if reason is SubtaskFailureReason.OUTPUT_CONTRACT:
        return FailureDiagnosis(
            reason=reason,
            retryable=True,
            evidence_artifact_ids=_uniq(evidence),
            concise_feedback=_truncate(message or "Output contract validation failed."),
            recommended_edit_types=["prompt_feedback", "contract_clarification"],
            infrastructure_related=False,
        )

    if reason is SubtaskFailureReason.TIMEOUT:
        return FailureDiagnosis(
            reason=reason,
            retryable=True,
            evidence_artifact_ids=_uniq(evidence),
            concise_feedback=_truncate(message or "Execution timed out."),
            recommended_edit_types=[
                "budget_adjustment",
                "prompt_simplification",
                "model_override",
            ],
            infrastructure_related=False,
        )

    if reason is SubtaskFailureReason.TOOL:
        return FailureDiagnosis(
            reason=reason,
            retryable=True,
            evidence_artifact_ids=_uniq(evidence),
            concise_feedback=_truncate(message or "Tool execution failed."),
            recommended_edit_types=["tool_policy", "prompt_feedback"],
            infrastructure_related=False,
        )

    if reason is SubtaskFailureReason.MODEL:
        return FailureDiagnosis(
            reason=reason,
            retryable=True,
            evidence_artifact_ids=_uniq(evidence),
            concise_feedback=_truncate(message or "Model backend failed."),
            recommended_edit_types=["fresh_retry", "model_override"],
            infrastructure_related=False,
        )

    if reason is SubtaskFailureReason.INFRA:
        return FailureDiagnosis(
            reason=reason,
            retryable=True,
            evidence_artifact_ids=_uniq(evidence),
            concise_feedback=_truncate(message or "Infrastructure failure."),
            recommended_edit_types=[],
            infrastructure_related=True,
        )

    if reason is SubtaskFailureReason.INVALID_CONFIG:
        return FailureDiagnosis(
            reason=reason,
            retryable=False,
            evidence_artifact_ids=_uniq(evidence),
            concise_feedback=_truncate(message or "Invalid configuration."),
            recommended_edit_types=[],
            infrastructure_related=False,
        )

    if node_results:
        for node_id, result in node_results.items():
            node = next((n for n in graph.nodes if n.node_id == node_id), None)
            if node is None:
                continue
            if node.node_kind is NodeKind.HARNESS and not result.succeeded:
                return FailureDiagnosis(
                    reason=SubtaskFailureReason.HARNESS,
                    retryable=True,
                    evidence_artifact_ids=_uniq(evidence),
                    concise_feedback=_truncate(result.error or message or "Harness failed."),
                    recommended_edit_types=[
                        "prompt_feedback",
                        "budget_adjustment",
                        "add_verifier_node",
                        "model_override",
                    ],
                )

    return FailureDiagnosis(
        reason=reason or SubtaskFailureReason.UNKNOWN,
        retryable=True,
        evidence_artifact_ids=_uniq(evidence),
        concise_feedback=_truncate(message or "Subtask failed."),
        recommended_edit_types=["prompt_feedback", "fresh_retry"],
        infrastructure_related=False,
    )


def _truncate(text: str, *, limit: int = 1200) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _uniq(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out
