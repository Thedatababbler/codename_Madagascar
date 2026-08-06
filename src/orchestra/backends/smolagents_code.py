"""Smolagents CodeAgent backend (worker-isolated).

Supports both FinalAnswerArtifact (BBEH) and RepositoryChangeArtifact
(RealBench repository editing). Repository changes are derived exclusively
from Git workspace evidence, never from model-authored patch text.
"""

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
from orchestra.backends.capabilities import BackendCapabilities, SessionPolicy
from orchestra.backends.errors import (
    BackendInitializationError,
    OutputContractValidationError,
    OutputParseError,
)
from orchestra.backends.workers.smolagents_worker import run_worker_process
from orchestra.ir.artifacts import create_artifact
from orchestra.llm.usage import LLMUsage
from orchestra.prompts.parsers import parse_output
from orchestra.schemas.artifacts import RepositoryChangeArtifact
from orchestra.workspaces.base import WorkspaceRef
from orchestra.workspaces.git_workspace import SharedSubtaskGitWorkspaceManager

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
        workspaces: SharedSubtaskGitWorkspaceManager | None = None,
    ) -> None:
        self._worker_runner = worker_runner
        self.default_wall_timeout_seconds = default_wall_timeout_seconds
        self._workspaces = workspaces or SharedSubtaskGitWorkspaceManager()

    @property
    def backend_id(self) -> str:
        return "smolagents_code"

    @property
    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            multi_step=True,
            code_actions=True,
            structured_tools=True,
            repository_editing=True,
            supports_remote_executor=True,
            supports_step_trace=True,
            supports_resume=False,
            supports_session_state=False,
            supported_session_policies=frozenset({SessionPolicy.FRESH}),
            supports_tool_policy_edit=True,
            supports_model_override=True,
            supports_workspace_rebinding=True,
            supports_parallel_instances=True,
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

    def _fail(
        self,
        request: AgentRequest,
        status: AgentRunStatus,
        message: str,
        *,
        latency_ms: int,
        metadata: dict[str, Any] | None = None,
        final_output: str | None = None,
        usage: LLMUsage | None = None,
        trace_events: list[AgentTraceEvent] | None = None,
        step_count: int = 0,
    ) -> AgentResult:
        return AgentResult(
            request_id=request.request_id,
            backend_id=self.backend_id,
            status=status,
            final_output=final_output,
            trace_events=list(trace_events or []),
            usage=usage or LLMUsage(),
            latency_ms=latency_ms,
            step_count=step_count,
            error=AgentError(status=status, message=message),
            backend_metadata=dict(metadata or {}),
        )

    def _build_worker_request(
        self, request: AgentRequest, context: BackendExecutionContext
    ) -> dict[str, Any]:
        fixture_responses = request.backend_config.get("fixture_responses")
        return {
            "run_id": context.run_id,
            "task_id": request.task_id,
            "subtask_id": context.subtask_id or request.subtask_id,
            "node_id": request.node_id,
            "workspace_ref": context.workspace_ref,
            "task": request.rendered_context or request.instruction,
            "instruction": request.instruction,
            "rendered_context": request.rendered_context,
            "model": request.model.model_dump(mode="json"),
            "tools": list(request.tools),
            "max_steps": request.max_steps,
            "backend_config": {
                key: value
                for key, value in dict(request.backend_config).items()
                if key != "fixture_responses"
            },
            "fixture_responses": list(fixture_responses or []),
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

    def _parse_final_output(self, request: AgentRequest, final_output: str | None):
        text = "" if final_output is None else str(final_output)
        try:
            parsed = parse_output(
                request.output_contract.parser_id,
                text,
                request.output_contract.output_schema,
                request.node_id,
            )
        except ValueError as exc:
            raise OutputParseError(str(exc)) from exc
        if type(parsed).__name__ != request.output_contract.output_schema:
            raise OutputContractValidationError(
                f"Parser produced {type(parsed).__name__}, "
                f"expected {request.output_contract.output_schema}"
            )
        extraction = getattr(parsed, "extraction_status", None)
        if extraction in {"empty", "malformed"}:
            raise OutputContractValidationError(
                f"Answer extraction failed: {extraction}"
            )
        return parsed

    def _usage_from_raw(self, usage_raw: dict[str, Any]) -> tuple[LLMUsage, dict[str, Any]]:
        """Build usage with explicit provenance; never invent missing fields."""
        provenance: dict[str, Any] = {"source": "smolagents_worker"}
        has_prompt = "prompt_tokens" in usage_raw or "input_tokens" in usage_raw
        has_completion = (
            "completion_tokens" in usage_raw or "output_tokens" in usage_raw
        )
        prompt = int(
            usage_raw.get("prompt_tokens", usage_raw.get("input_tokens", 0)) or 0
        )
        completion = int(
            usage_raw.get("completion_tokens", usage_raw.get("output_tokens", 0)) or 0
        )
        if "total_tokens" in usage_raw:
            total = int(usage_raw.get("total_tokens") or 0)
            provenance["total_tokens"] = "provider"
        else:
            total = prompt + completion
            provenance["total_tokens"] = (
                "derived_sum" if (has_prompt or has_completion) else "unavailable"
            )
        provenance["prompt_tokens"] = "provider" if has_prompt else "unavailable"
        provenance["completion_tokens"] = (
            "provider" if has_completion else "unavailable"
        )
        return (
            LLMUsage(
                prompt_tokens=prompt,
                completion_tokens=completion,
                total_tokens=total,
            ),
            provenance,
        )

    async def _build_repository_change_artifact(
        self,
        *,
        request: AgentRequest,
        context: BackendExecutionContext,
        final_output: str | None,
        base_revision: str | None,
    ) -> RepositoryChangeArtifact:
        if not context.workspace_ref:
            raise OutputContractValidationError(
                "smolagents repository editing requires workspace_ref"
            )
        workspace = WorkspaceRef(
            workspace_id=context.subtask_id or request.task_id,
            path=context.workspace_ref,
            task_id=context.task_id,
            subtask_id=context.subtask_id or "main",
            base_revision=base_revision,
        )
        snap = await self._workspaces.snapshot(workspace)
        # Deterministic ordering of changed files.
        changed = sorted({str(p).replace("\\", "/") for p in snap.changed_files})
        require_diff = bool(request.backend_config.get("require_git_diff", True))
        if require_diff and not (snap.patch or "").strip() and not changed:
            raise OutputContractValidationError(
                "require_git_diff=true but git diff is empty; refusing model text "
                "as repository change evidence"
            )
        thread_id = f"smolagents:{request.request_id}"
        return RepositoryChangeArtifact(
            workspace_ref=workspace.path,
            thread_id=thread_id,
            base_revision=base_revision or snap.head_revision,
            changed_files=changed,
            patch=snap.patch or "",
            final_response="" if final_output is None else str(final_output),
            source_node=request.node_id,
        )

    async def run(
        self,
        request: AgentRequest,
        context: BackendExecutionContext,
    ) -> AgentResult:
        started = time.perf_counter()
        if request.backend_config.get("managed_agents") not in (None,):
            return self._fail(
                request,
                AgentRunStatus.BACKEND_INIT_FAILURE,
                "managed_agents is forbidden",
                latency_ms=0,
            )

        wants_repo = (
            request.output_contract.output_schema == "RepositoryChangeArtifact"
        )
        if wants_repo and not context.workspace_ref:
            return self._fail(
                request,
                AgentRunStatus.INVALID_REQUEST,
                "smolagents_code repository editing requires workspace_ref; "
                "refusing process-cwd inference",
                latency_ms=0,
            )

        base_revision: str | None = None
        if wants_repo and context.workspace_ref:
            workspace = WorkspaceRef(
                workspace_id=context.subtask_id or request.task_id,
                path=context.workspace_ref,
                task_id=context.task_id,
                subtask_id=context.subtask_id or "main",
                base_revision=None,
            )
            try:
                snap_before = await self._workspaces.snapshot(workspace)
                base_revision = snap_before.head_revision or snap_before.base_revision
            except Exception as exc:  # noqa: BLE001
                return self._fail(
                    request,
                    AgentRunStatus.INFRA_ERROR,
                    f"pre-edit workspace snapshot failed: {type(exc).__name__}: {exc}",
                    latency_ms=int((time.perf_counter() - started) * 1000),
                )

        payload = self._build_worker_request(request, context)
        wall_timeout = float(
            request.timeout_seconds or self.default_wall_timeout_seconds
        )

        def _invoke() -> dict[str, Any]:
            if self._worker_runner is not None:
                return self._worker_runner(payload)
            return run_worker_process(payload, wall_timeout_seconds=wall_timeout)

        try:
            raw = await asyncio.to_thread(_invoke)
        except Exception as exc:  # noqa: BLE001
            return self._fail(
                request,
                AgentRunStatus.INFRA_ERROR,
                f"{type(exc).__name__}: {exc}",
                latency_ms=int((time.perf_counter() - started) * 1000),
            )

        status = _STATUS_MAP.get(
            str(raw.get("status") or "infra_error"), AgentRunStatus.INFRA_ERROR
        )
        usage, usage_provenance = self._usage_from_raw(dict(raw.get("usage") or {}))
        trace_events = self._parse_trace(list(raw.get("trace_events") or []))
        latency_ms = int((time.perf_counter() - started) * 1000)
        metadata = dict(raw.get("backend_metadata") or {})
        metadata["trace_dir"] = context.trace_dir
        metadata["usage_provenance"] = usage_provenance
        metadata["workspace_ref"] = context.workspace_ref
        metadata["backend"] = self.backend_id
        if request.model is not None:
            metadata["model"] = request.model.model_dump(mode="json")
        final_output = raw.get("final_output")
        step_count = int(raw.get("step_count") or 0)
        if status is not AgentRunStatus.SUCCESS:
            return self._fail(
                request,
                status,
                str(raw.get("error") or status.value),
                latency_ms=latency_ms,
                metadata=metadata,
                final_output=final_output,
                usage=usage,
                trace_events=trace_events,
                step_count=step_count,
            )

        try:
            if wants_repo:
                # Canonical evidence from filesystem/Git — ignore model patch claims.
                parsed = await self._build_repository_change_artifact(
                    request=request,
                    context=context,
                    final_output=final_output,
                    base_revision=base_revision,
                )
                metadata["change_evidence"] = "git_workspace_snapshot"
                metadata["model_patch_ignored"] = True
            else:
                parsed = self._parse_final_output(request, final_output)
        except OutputParseError as exc:
            return self._fail(
                request,
                AgentRunStatus.ACTION_PARSE_FAILURE,
                f"{type(exc).__name__}: {exc}",
                latency_ms=latency_ms,
                metadata=metadata,
                final_output=final_output,
                usage=usage,
                trace_events=trace_events,
                step_count=step_count,
            )
        except OutputContractValidationError as exc:
            return self._fail(
                request,
                AgentRunStatus.OUTPUT_CONTRACT_FAILURE,
                f"{type(exc).__name__}: {exc}",
                latency_ms=latency_ms,
                metadata=metadata,
                final_output=final_output,
                usage=usage,
                trace_events=trace_events,
                step_count=step_count,
            )
        except Exception as exc:  # noqa: BLE001
            return self._fail(
                request,
                AgentRunStatus.INFRA_ERROR,
                f"post-edit evidence failed: {type(exc).__name__}: {exc}",
                latency_ms=latency_ms,
                metadata=metadata,
                final_output=final_output,
                usage=usage,
                trace_events=trace_events,
                step_count=step_count,
            )

        if hasattr(parsed, "source_node"):
            parsed = parsed.model_copy(update={"source_node": request.node_id})
        artifact = create_artifact(
            parsed,
            producer_node_id=request.node_id,
            task_id=request.task_id,
            parent_artifact_ids=[ref.artifact_id for ref in context.artifact_refs],
        )
        return AgentResult(
            request_id=request.request_id,
            backend_id=self.backend_id,
            status=AgentRunStatus.SUCCESS,
            final_output=final_output,
            output_artifacts=[artifact],
            trace_events=trace_events,
            usage=usage,
            latency_ms=latency_ms,
            step_count=step_count,
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
