"""Failure diagnosis for Fast Loop candidate generation."""

from __future__ import annotations

from orchestra.control.fast_loop.schemas import FailureDiagnosis
from orchestra.control.task_state import SubtaskFailureReason, SubtaskState, SubtaskStatus
from orchestra.harness.progress import behaviour_failures
from orchestra.ir.graph import OrchestraGraph
from orchestra.ir.nodes import NodeKind
from orchestra.runtime.state import GraphExecutionResult, NodeExecutionResult, NodeStatus


def _failed_nodes_from_results(
    node_results: dict[str, NodeExecutionResult] | None,
    graph: OrchestraGraph,
) -> list[str]:
    if not node_results:
        return []
    failed: list[str] = []
    for node_id, result in node_results.items():
        if not result.succeeded:
            failed.append(node_id)
    return failed


def _failed_nodes_from_graph_result(
    result: GraphExecutionResult | None,
) -> list[str]:
    if result is None:
        return []
    failed: list[str] = []
    for node_id, status in result.state.node_status.items():
        if status is NodeStatus.FAILED:
            failed.append(node_id)
    # Prefer agent/harness failures over skipped.
    return failed


def _primary_failed_node(
    failed_ids: list[str],
    graph: OrchestraGraph,
) -> str | None:
    if not failed_ids:
        return None
    by_id = {n.node_id: n for n in graph.nodes}
    # Prefer harness, then agent, then others — last failing agent in topo-ish order.
    harness = [i for i in failed_ids if by_id.get(i) and by_id[i].node_kind is NodeKind.HARNESS]
    agents = [i for i in failed_ids if by_id.get(i) and by_id[i].node_kind is NodeKind.AGENT]
    if agents:
        # When harness also failed, edit the agent that produced harness input
        # (typically the last agent before harness).
        return agents[-1]
    if harness:
        # Attribute harness failure to nearest upstream agent via edges.
        for edge in graph.edges:
            if edge.destination_node in harness and edge.source_node in by_id:
                if by_id[edge.source_node].node_kind is NodeKind.AGENT:
                    return edge.source_node
        return harness[0]
    return failed_ids[0]


def diagnose_subtask_failure(
    *,
    subtask_state: SubtaskState,
    graph: OrchestraGraph,
    node_results: dict[str, NodeExecutionResult] | None = None,
    graph_result: GraphExecutionResult | None = None,
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

    failed_ids = _failed_nodes_from_results(node_results, graph)
    if not failed_ids:
        failed_ids = _failed_nodes_from_graph_result(graph_result)
    # Harness failure without node results: infer agent→harness lineage.
    if not failed_ids and reason is SubtaskFailureReason.HARNESS:
        harness_ids = [
            n.node_id for n in graph.nodes if n.node_kind is NodeKind.HARNESS
        ]
        failed_ids = list(harness_ids)
        for edge in graph.edges:
            if edge.destination_node in harness_ids:
                src = next((n for n in graph.nodes if n.node_id == edge.source_node), None)
                if src and src.node_kind is NodeKind.AGENT and src.node_id not in failed_ids:
                    failed_ids.insert(0, src.node_id)

    primary = _primary_failed_node(failed_ids, graph)

    # Recorded on the attempt by the scheduler when the harness reported a graded
    # result. Carried onto the diagnosis so candidate generation can pick an edit
    # aimed at the stage that failed.
    furthest_stage = ""
    named_failures: list[str] = []
    for attempt in subtask_state.attempts:
        meta = attempt.metadata or {}
        stage = meta.get("furthest_stage")
        if stage:
            furthest_stage = str(stage)
        stages = meta.get("harness_stages")
        if stages:
            named_failures = behaviour_failures(stages)

    base_kwargs = {
        "reason": reason,
        "evidence_artifact_ids": _uniq(evidence),
        "failed_node_ids": failed_ids,
        "primary_failed_node_id": primary,
        "furthest_stage": furthest_stage,
        "behaviour_failures": named_failures,
    }

    if reason is SubtaskFailureReason.HARNESS:
        return FailureDiagnosis(
            **base_kwargs,
            retryable=True,
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
            **base_kwargs,
            retryable=True,
            concise_feedback=_truncate(message or "Output contract validation failed."),
            recommended_edit_types=["prompt_feedback", "contract_clarification"],
            infrastructure_related=False,
        )

    if reason is SubtaskFailureReason.TIMEOUT:
        return FailureDiagnosis(
            **base_kwargs,
            retryable=True,
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
            **base_kwargs,
            retryable=True,
            concise_feedback=_truncate(message or "Tool execution failed."),
            recommended_edit_types=["tool_policy", "prompt_feedback"],
            infrastructure_related=False,
        )

    if reason is SubtaskFailureReason.MODEL:
        return FailureDiagnosis(
            **base_kwargs,
            retryable=True,
            concise_feedback=_truncate(message or "Model backend failed."),
            recommended_edit_types=["fresh_retry", "model_override"],
            infrastructure_related=False,
        )

    if reason is SubtaskFailureReason.INFRA:
        return FailureDiagnosis(
            **base_kwargs,
            retryable=True,
            concise_feedback=_truncate(message or "Infrastructure failure."),
            recommended_edit_types=[],
            infrastructure_related=True,
        )

    if reason is SubtaskFailureReason.INVALID_CONFIG:
        return FailureDiagnosis(
            **base_kwargs,
            retryable=False,
            concise_feedback=_truncate(message or "Invalid configuration."),
            recommended_edit_types=[],
            infrastructure_related=False,
        )

    # base_kwargs already carries `reason`; passing it again raised TypeError on
    # every failure whose reason matched none of the branches above, so the
    # catch-all crashed instead of catching.
    return FailureDiagnosis(
        **{**base_kwargs, "reason": reason or SubtaskFailureReason.UNKNOWN},
        retryable=True,
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
