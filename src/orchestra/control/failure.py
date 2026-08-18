"""Map graph/backend outcomes to SubtaskStatus + SubtaskFailureReason."""

from __future__ import annotations

from orchestra.backends.base import AgentRunStatus
from orchestra.control.task_state import SubtaskFailureReason, SubtaskStatus
from orchestra.harness.progress import MARKER as SCORE_MARKER
from orchestra.runtime.errors import GraphDeadlockError
from orchestra.runtime.state import GraphExecutionResult, NodeStatus
from orchestra.schemas.artifacts import RepositoryHarnessResultArtifact
from orchestra.storage.artifacts import ArtifactStore

_STATUS_TO_REASON: dict[AgentRunStatus, SubtaskFailureReason] = {
    AgentRunStatus.MODEL_FAILURE: SubtaskFailureReason.MODEL,
    AgentRunStatus.TOOL_FAILURE: SubtaskFailureReason.TOOL,
    AgentRunStatus.ACTION_PARSE_FAILURE: SubtaskFailureReason.TOOL,
    AgentRunStatus.OUTPUT_CONTRACT_FAILURE: SubtaskFailureReason.OUTPUT_CONTRACT,
    AgentRunStatus.TIMEOUT: SubtaskFailureReason.TIMEOUT,
    AgentRunStatus.MAX_STEPS_EXCEEDED: SubtaskFailureReason.MODEL,
    AgentRunStatus.INVALID_REQUEST: SubtaskFailureReason.INVALID_CONFIG,
    AgentRunStatus.BACKEND_UNAVAILABLE: SubtaskFailureReason.INFRA,
    AgentRunStatus.BACKEND_INIT_FAILURE: SubtaskFailureReason.INFRA,
    AgentRunStatus.INFRA_ERROR: SubtaskFailureReason.INFRA,
    AgentRunStatus.CANCELLED: SubtaskFailureReason.INFRA,
}


async def _harness_failed_message(
    *,
    result: GraphExecutionResult | None,
    artifact_store: ArtifactStore,
) -> str | None:
    if result is None:
        return None
    for _node_id, outputs in result.state.node_outputs.items():
        for artifact_id in outputs.values():
            try:
                art = await artifact_store.get(artifact_id)
            except KeyError:
                continue
            if art.artifact_type != "RepositoryHarnessResultArtifact":
                continue
            payload = RepositoryHarnessResultArtifact.model_validate(art.payload)
            if not payload.passed:
                detail = _readable_detail(
                    payload.stderr_summary or payload.stdout_summary or ""
                )
                return detail or "repository harness reported passed=false"
    return None


def _readable_detail(text: str) -> str:
    """The part of a harness report a prompt may quote back to an agent.

    The score marker is machine data — a JSON line listing every stage and the
    identity of every test that failed, including the authored suite the
    implementer is deliberately not allowed to read. It reaches the selector
    through ``parse_progress``; letting it also reach the next candidate's
    prompt would hand that candidate the yardstick.
    """
    kept = [line for line in text.splitlines() if not line.startswith(SCORE_MARKER)]
    return "\n".join(kept).strip()


def reason_from_backend_status(
    status: AgentRunStatus | None,
) -> SubtaskFailureReason:
    if status is None:
        return SubtaskFailureReason.UNKNOWN
    return _STATUS_TO_REASON.get(status, SubtaskFailureReason.UNKNOWN)


async def classify_subtask_outcome(
    *,
    result: GraphExecutionResult | None,
    artifact_store: ArtifactStore,
    error: BaseException | None = None,
) -> tuple[SubtaskStatus, SubtaskFailureReason | None, str | None]:
    """Return (status, failure_reason, failure_message) for a finished attempt."""
    if result is not None and result.state.frozen and result.state.final_output_artifact_id:
        return SubtaskStatus.COMMITTED, None, None

    harness_msg = await _harness_failed_message(
        result=result, artifact_store=artifact_store
    )
    if harness_msg is not None:
        return SubtaskStatus.HARNESS_FAILED, SubtaskFailureReason.HARNESS, harness_msg

    if result is not None:
        for node_id, status in result.state.node_status.items():
            if status is not NodeStatus.FAILED:
                continue
            meta = result.state.node_backend_metadata.get(node_id) or {}
            backend_status_raw = meta.get("backend_status")
            backend_status = None
            if backend_status_raw:
                try:
                    backend_status = AgentRunStatus(backend_status_raw)
                except ValueError:
                    backend_status = None
            reason = reason_from_backend_status(backend_status)
            message = str(meta.get("error") or f"node {node_id} failed")
            return SubtaskStatus.FAILED, reason, message

    if isinstance(error, GraphDeadlockError):
        return (
            SubtaskStatus.FAILED,
            SubtaskFailureReason.INFRA,
            f"{type(error).__name__}: {error}",
        )
    if error is not None:
        return (
            SubtaskStatus.FAILED,
            SubtaskFailureReason.INFRA,
            f"{type(error).__name__}: {error}",
        )
    return (
        SubtaskStatus.FAILED,
        SubtaskFailureReason.UNKNOWN,
        "graph execution did not freeze with final output",
    )
