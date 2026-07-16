"""Codex Python SDK backend (Milestone 3.5 vertical slice)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from orchestra.backends.base import (
    AgentError,
    AgentRequest,
    AgentResult,
    AgentRunStatus,
    AgentSessionPolicy,
    BackendExecutionContext,
    BackendHealth,
    BackendSessionRef,
)
from orchestra.backends.capabilities import BackendCapabilities
from orchestra.backends.codex_types import map_approval_policy, map_sandbox
from orchestra.ir.artifacts import create_artifact
from orchestra.llm.usage import LLMUsage
from orchestra.schemas.artifacts import RepositoryChangeArtifact
from orchestra.workspaces.base import WorkspaceRef
from orchestra.workspaces.git_workspace import SharedSubtaskGitWorkspaceManager

CodexClientFactory = Callable[[], Any]


class CodexSDKBackend:
    backend_id = "codex_sdk"

    def __init__(
        self,
        *,
        client_factory: CodexClientFactory | None = None,
        workspace_manager: SharedSubtaskGitWorkspaceManager | None = None,
    ) -> None:
        self._client_factory = client_factory
        self._workspaces = workspace_manager or SharedSubtaskGitWorkspaceManager()

    @property
    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            multi_step=True,
            code_actions=True,
            structured_tools=False,
            repository_editing=True,
            supports_remote_executor=False,
            supports_step_trace=False,
            supports_resume=False,
        )

    async def healthcheck(self) -> BackendHealth:
        try:
            import openai_codex  # noqa: F401
            from openai_codex import AsyncCodex, Sandbox  # noqa: F401
        except ImportError as exc:
            return BackendHealth(
                healthy=False,
                backend_id=self.backend_id,
                detail=(
                    f"openai_codex missing: {exc}; install with: uv sync --extra codex"
                ),
            )
        try:
            # Importing and constructing validates bundled runtime packaging.
            if self._client_factory is not None:
                return BackendHealth(healthy=True, backend_id=self.backend_id)
            from openai_codex import AsyncCodex

            async with AsyncCodex() as client:
                _ = client
            return BackendHealth(
                healthy=True,
                backend_id=self.backend_id,
                detail=(
                    "openai-codex import ok; bundled runtime present; "
                    "workspace_write sandbox available"
                ),
            )
        except Exception as exc:  # noqa: BLE001
            return BackendHealth(
                healthy=False,
                backend_id=self.backend_id,
                detail=f"Codex runtime/auth healthcheck failed: {type(exc).__name__}: {exc}",
            )

    def _fail(
        self,
        request: AgentRequest,
        status: AgentRunStatus,
        message: str,
        *,
        latency_ms: int = 0,
        metadata: dict[str, Any] | None = None,
    ) -> AgentResult:
        return AgentResult(
            request_id=request.request_id,
            backend_id=self.backend_id,
            status=status,
            error=AgentError(status=status, message=message),
            latency_ms=latency_ms,
            backend_metadata=dict(metadata or {}),
        )

    async def run(
        self,
        request: AgentRequest,
        context: BackendExecutionContext,
    ) -> AgentResult:
        started = time.perf_counter()
        if request.session_policy is not AgentSessionPolicy.FRESH:
            return self._fail(
                request,
                AgentRunStatus.INVALID_REQUEST,
                "M3.5 only supports session_policy=fresh "
                f"(got {request.session_policy.value})",
            )
        if request.backend_config.get("thread_policy", "fresh") != "fresh":
            return self._fail(
                request,
                AgentRunStatus.INVALID_REQUEST,
                "Codex thread_policy must be fresh in M3.5",
            )
        if not context.workspace_ref:
            return self._fail(
                request,
                AgentRunStatus.INVALID_REQUEST,
                "codex_sdk requires workspace_ref; refusing implicit cwd/project-root fallback",
            )

        workspace = WorkspaceRef(
            workspace_id=context.subtask_id or request.task_id,
            path=context.workspace_ref,
            task_id=context.task_id,
            subtask_id=context.subtask_id or "main",
            base_revision=None,
        )
        # Refresh base revision from the live workspace.
        try:
            snap_before = await self._workspaces.snapshot(workspace)
            workspace = workspace.model_copy(
                update={"base_revision": snap_before.head_revision or snap_before.base_revision}
            )
        except Exception as exc:  # noqa: BLE001
            return self._fail(
                request,
                AgentRunStatus.INFRA_ERROR,
                f"workspace snapshot failed: {type(exc).__name__}: {exc}",
                latency_ms=int((time.perf_counter() - started) * 1000),
            )

        sandbox_name = str(request.backend_config.get("sandbox") or "workspace_write")
        approval_name = str(request.backend_config.get("approval_policy") or "never")
        require_diff = bool(request.backend_config.get("require_git_diff", True))
        prompt = request.rendered_context or request.instruction

        try:
            sandbox = map_sandbox(sandbox_name)
            approval = map_approval_policy(approval_name)
        except Exception as exc:  # noqa: BLE001
            return self._fail(
                request,
                AgentRunStatus.INVALID_REQUEST,
                str(exc),
                latency_ms=int((time.perf_counter() - started) * 1000),
            )

        client_factory = self._client_factory
        if client_factory is None:
            try:
                from openai_codex import AsyncCodex
            except ImportError as exc:
                return self._fail(
                    request,
                    AgentRunStatus.BACKEND_UNAVAILABLE,
                    f"openai_codex not installed: {exc}",
                    latency_ms=int((time.perf_counter() - started) * 1000),
                )

            def client_factory() -> Any:
                return AsyncCodex()

        timeout = float(request.timeout_seconds or 300.0)
        thread_id = ""
        final_response = ""
        usage = LLMUsage()
        try:
            async with asyncio.timeout(timeout):
                client = client_factory()
                close = getattr(client, "close", None)
                try:
                    if hasattr(client, "__aenter__"):
                        client = await client.__aenter__()
                    thread = await client.thread_start(
                        cwd=workspace.path,
                        sandbox=sandbox,
                        approval_mode=approval,
                        model=request.model.name if request.model else None,
                    )
                    thread_id = str(getattr(thread, "id", "") or "")
                    turn = await thread.run(prompt, sandbox=sandbox, approval_mode=approval)
                    final_response = str(getattr(turn, "final_response", "") or "")
                    turn_usage = getattr(turn, "usage", None)
                    if turn_usage is not None:
                        usage = LLMUsage(
                            prompt_tokens=int(
                                getattr(turn_usage, "input_tokens", 0)
                                or getattr(turn_usage, "prompt_tokens", 0)
                                or 0
                            ),
                            completion_tokens=int(
                                getattr(turn_usage, "output_tokens", 0)
                                or getattr(turn_usage, "completion_tokens", 0)
                                or 0
                            ),
                        )
                finally:
                    if hasattr(client, "__aexit__"):
                        await client.__aexit__(None, None, None)
                    elif callable(close):
                        result = close()
                        if asyncio.iscoroutine(result) or isinstance(result, Awaitable):
                            await result  # type: ignore[misc]
        except TimeoutError:
            return self._fail(
                request,
                AgentRunStatus.TIMEOUT,
                f"Codex exceeded timeout_seconds={timeout}",
                latency_ms=int((time.perf_counter() - started) * 1000),
                metadata={"workspace_ref": workspace.path},
            )
        except Exception as exc:  # noqa: BLE001
            return self._fail(
                request,
                AgentRunStatus.MODEL_FAILURE,
                f"{type(exc).__name__}: {exc}",
                latency_ms=int((time.perf_counter() - started) * 1000),
                metadata={"workspace_ref": workspace.path, "thread_id": thread_id},
            )

        try:
            snap = await self._workspaces.snapshot(workspace)
        except Exception as exc:  # noqa: BLE001
            return self._fail(
                request,
                AgentRunStatus.INFRA_ERROR,
                f"post-edit git snapshot failed: {type(exc).__name__}: {exc}",
                latency_ms=int((time.perf_counter() - started) * 1000),
            )

        if require_diff and not snap.patch.strip():
            return self._fail(
                request,
                AgentRunStatus.OUTPUT_CONTRACT_FAILURE,
                "require_git_diff=true but git diff is empty; refusing model text as code source",
                latency_ms=int((time.perf_counter() - started) * 1000),
                metadata={
                    "workspace_ref": workspace.path,
                    "thread_id": thread_id,
                    "final_response": final_response[:500],
                },
            )

        expected_schema = request.output_contract.output_schema
        if expected_schema != "RepositoryChangeArtifact":
            return self._fail(
                request,
                AgentRunStatus.OUTPUT_CONTRACT_FAILURE,
                f"codex_sdk produces RepositoryChangeArtifact, got contract {expected_schema}",
                latency_ms=int((time.perf_counter() - started) * 1000),
            )

        payload = RepositoryChangeArtifact(
            workspace_ref=workspace.path,
            thread_id=thread_id,
            base_revision=workspace.base_revision,
            changed_files=list(snap.changed_files),
            patch=snap.patch,
            final_response=final_response,
            source_node=request.node_id,
        )
        artifact = create_artifact(
            payload,
            producer_node_id=request.node_id,
            task_id=request.task_id,
        )
        session_ref = BackendSessionRef(
            backend_id=self.backend_id,
            session_id=thread_id or request.request_id,
            parent_session_id=None,
        )
        return AgentResult(
            request_id=request.request_id,
            backend_id=self.backend_id,
            status=AgentRunStatus.SUCCESS,
            final_output=final_response,
            output_artifacts=[artifact],
            usage=usage,
            latency_ms=int((time.perf_counter() - started) * 1000),
            step_count=1,
            session_ref=session_ref,
            backend_metadata={
                "workspace_ref": workspace.path,
                "thread_id": thread_id,
                "changed_files": list(snap.changed_files),
                "base_revision": workspace.base_revision,
            },
        )
