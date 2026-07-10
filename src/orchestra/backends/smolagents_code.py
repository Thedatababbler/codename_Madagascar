"""Smolagents CodeAgent backend (worker-isolated)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

from orchestra.backends.base import (
    AgentBackend,
    AgentError,
    AgentRequest,
    AgentResult,
    AgentRunStatus,
    AgentTraceEvent,
    BackendExecutionContext,
    BackendHealth,
)
from orchestra.backends.capabilities import BackendCapabilities
from orchestra.backends.errors import BackendInitializationError
from orchestra.backends.workers.smolagents_worker import run_worker_process
from orchestra.ir.artifacts import create_artifact
from orchestra.llm.usage import LLMUsage
from orchestra.schemas.artifacts import FinalAnswerArtifact

WorkerRunner = Callable[[dict[str, Any]], dict[str, Any]]

_STATUS_MAP = {
    "success": AgentRunStatus.SUCCESS,
    "invalid_request": AgentRunStatus.INVALID_REQUEST,
    "backend_unavailable": AgentRunStatus.BACKEND_UNAVAILABLE,
    "backend_init_failure": AgentRunStatus.BACKEND_INIT_FAILURE,
    "model_failure": AgentRunStatus.MODEL_FAILURE,
    "action_parse_failure": AgentRunStatus.ACTION_PARSE_FAILURE,
    "tool_failure": AgentRunStatus.TOOL_FAILURE,
    "max_steps_exceeded": AgentRunStatus.MAX_STEPS_EXCEEDED,
    "output_contract_failure": AgentRunStatus.OUTPUT_CONTRACT_FAILURE,
    "timeout": AgentRunStatus.TIMEOUT,
    "cancelled": AgentRunStatus.CANCELLED,
    "infra_error": AgentRunStatus.INFRA_ERROR,
}


class SmolagentsCodeBackend:
    def __init__(
        self,
        *,
        worker_runner: WorkerRunner | None = None,
        default_wall_timeout_seconds: float = 300.0,
    ) -> None:
        self._worker_runner = worker_runner
        self.default_wall_timeout_seconds = default_wall_timeout_seconds

    @property
    def backend_id(self) -> str:
        return "smolagents_code"

    @property
    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            multi_step=True,
            code_actions=True,
            structured_tools=True,
            repository_editing=False,
            supports_remote_executor=True,
            supports_step_trace=True,
            supports_resume=False,
        )

    async def healthcheck(self) -> BackendHealth:
        try:
            import smolagents  # noqa: F401
            from smolagents import OpenAIServerModel  # noqa: F401
        except ImportError as exc:
            return BackendHealth(
                healthy=False,
                backend_id=self.backend_id,
                detail=(
                    f"smolagents/openai missing: {exc}; "
                    "install with: uv sync --extra smolagents"
                ),
            )
        return BackendHealth(healthy=True, backend_id=self.backend_id)

    def _build_worker_request(
        self, request: AgentRequest, context: BackendExecutionContext
    ) -> dict[str, Any]:
        return {
            "run_id": context.run_id,
            "task_id": request.task_id,
            "node_id": request.node_id,
            "task": request.rendered_context or request.instruction,
            "instruction": request.instruction,
            "rendered_context": request.rendered_context,
            "model": request.model.model_dump(mode="json"),
            "tools": list(request.tools),
            "max_steps": request.max_steps,
            "backend_config": dict(request.backend_config),
            "managed_agents": None,
            "timeout_seconds": request.timeout_seconds,
        }

    def _parse_trace(self, raw_events: list[dict[str, Any]]) -> list[AgentTraceEvent]:
        events: list[AgentTraceEvent] = []
        for item in raw_events:
            timestamp = item.get("timestamp")
            parsed_ts = None
            if isinstance(timestamp, str):
                try:
                    parsed_ts = datetime.fromisoformat(timestamp)
                except ValueError:
                    parsed_ts = None
            events.append(
                AgentTraceEvent(
                    event_type=str(item.get("event_type") or "action"),
                    index=item.get("index"),
                    timestamp=parsed_ts,
                    summary=item.get("summary"),
                    message=item.get("message"),
                    payload_ref=item.get("payload_ref"),
                    token_usage=dict(item.get("token_usage") or {}),
                    metadata=dict(item.get("metadata") or {}),
                )
            )
        return events

    def _extract_answer(self, final_output: str | None) -> FinalAnswerArtifact:
        if final_output is None or not str(final_output).strip():
            return FinalAnswerArtifact(
                answer="",
                raw_output=final_output,
                extraction_status="empty",
            )
        text = str(final_output).strip()
        answer = text.splitlines()[0].strip()
        if not answer:
            return FinalAnswerArtifact(
                answer="",
                raw_output=text,
                extraction_status="malformed",
            )
        return FinalAnswerArtifact(
            answer=answer,
            raw_output=text,
            extraction_status="ok",
        )

    async def run(
        self,
        request: AgentRequest,
        context: BackendExecutionContext,
    ) -> AgentResult:
        started = time.perf_counter()
        if request.backend_config.get("managed_agents") not in (None,):
            return AgentResult(
                request_id=request.request_id,
                backend_id=self.backend_id,
                status=AgentRunStatus.BACKEND_INIT_FAILURE,
                error=AgentError(
                    status=AgentRunStatus.BACKEND_INIT_FAILURE,
                    message="managed_agents is forbidden",
                ),
                latency_ms=0,
            )
        payload = self._build_worker_request(request, context)
        wall_timeout = float(request.timeout_seconds or self.default_wall_timeout_seconds)

        def _invoke() -> dict[str, Any]:
            if self._worker_runner is not None:
                return self._worker_runner(payload)
            return run_worker_process(payload, wall_timeout_seconds=wall_timeout)

        try:
            raw = await asyncio.to_thread(_invoke)
        except Exception as exc:  # noqa: BLE001
            return AgentResult(
                request_id=request.request_id,
                backend_id=self.backend_id,
                status=AgentRunStatus.INFRA_ERROR,
                error=AgentError(
                    status=AgentRunStatus.INFRA_ERROR,
                    message=f"{type(exc).__name__}: {exc}",
                ),
                latency_ms=int((time.perf_counter() - started) * 1000),
            )

        status = _STATUS_MAP.get(
            str(raw.get("status") or "infra_error"), AgentRunStatus.INFRA_ERROR
        )
        usage_raw = raw.get("usage") or {}
        usage = LLMUsage(
            prompt_tokens=int(usage_raw.get("prompt_tokens", 0) or 0),
            completion_tokens=int(usage_raw.get("completion_tokens", 0) or 0),
            total_tokens=int(
                usage_raw.get(
                    "total_tokens",
                    int(usage_raw.get("prompt_tokens", 0) or 0)
                    + int(usage_raw.get("completion_tokens", 0) or 0),
                )
                or 0
            ),
        )
        trace_events = self._parse_trace(list(raw.get("trace_events") or []))
        latency_ms = int((time.perf_counter() - started) * 1000)
        metadata = dict(raw.get("backend_metadata") or {})
        metadata["trace_dir"] = context.trace_dir
        if status is not AgentRunStatus.SUCCESS:
            return AgentResult(
                request_id=request.request_id,
                backend_id=self.backend_id,
                status=status,
                final_output=raw.get("final_output"),
                trace_events=trace_events,
                usage=usage,
                latency_ms=latency_ms,
                step_count=int(raw.get("step_count") or 0),
                error=AgentError(
                    status=status,
                    message=str(raw.get("error") or status.value),
                ),
                backend_metadata=metadata,
            )

        answer = self._extract_answer(raw.get("final_output"))
        if request.output_contract.output_schema == "FinalAnswerArtifact":
            if answer.extraction_status != "ok":
                return AgentResult(
                    request_id=request.request_id,
                    backend_id=self.backend_id,
                    status=AgentRunStatus.OUTPUT_CONTRACT_FAILURE,
                    final_output=raw.get("final_output"),
                    trace_events=trace_events,
                    usage=usage,
                    latency_ms=latency_ms,
                    step_count=int(raw.get("step_count") or 0),
                    error=AgentError(
                        status=AgentRunStatus.OUTPUT_CONTRACT_FAILURE,
                        message=f"Answer extraction failed: {answer.extraction_status}",
                    ),
                    backend_metadata=metadata,
                )
            answer = answer.model_copy(update={"source_node": request.node_id})
            artifact = create_artifact(
                answer,
                producer_node_id=request.node_id,
                task_id=request.task_id,
                parent_artifact_ids=[ref.artifact_id for ref in context.artifact_refs],
            )
        else:
            return AgentResult(
                request_id=request.request_id,
                backend_id=self.backend_id,
                status=AgentRunStatus.OUTPUT_CONTRACT_FAILURE,
                error=AgentError(
                    status=AgentRunStatus.OUTPUT_CONTRACT_FAILURE,
                    message=(
                        "smolagents_code currently supports FinalAnswerArtifact only, "
                        f"got {request.output_contract.output_schema}"
                    ),
                ),
                latency_ms=latency_ms,
                usage=usage,
                trace_events=trace_events,
                backend_metadata=metadata,
            )
        return AgentResult(
            request_id=request.request_id,
            backend_id=self.backend_id,
            status=AgentRunStatus.SUCCESS,
            final_output=raw.get("final_output"),
            output_artifacts=[artifact],
            trace_events=trace_events,
            usage=usage,
            latency_ms=latency_ms,
            step_count=int(raw.get("step_count") or 0),
            backend_metadata=metadata,
        )


def as_agent_backend(backend: SmolagentsCodeBackend) -> AgentBackend:
    return backend


def require_smolagents() -> None:
    try:
        import smolagents  # noqa: F401
    except ImportError as exc:
        raise BackendInitializationError(
            "smolagents is not installed; install with: uv sync --extra smolagents"
        ) from exc
