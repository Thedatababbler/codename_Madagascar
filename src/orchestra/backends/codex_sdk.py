"""Codex Python SDK backend (Milestone 3.5 vertical slice)."""

from __future__ import annotations

import asyncio
import os
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
from orchestra.backends.capabilities import BackendCapabilities, SessionPolicy
from orchestra.backends.codex_lifecycle import (
    CodexLifecycleError,
    CodexThreadLifecycleAdapter,
    sdk_supports_resume_fork,
)
from orchestra.backends.codex_types import map_approval_policy, map_sandbox
from orchestra.backends.exception_mapping import map_codex_exception
from orchestra.ir.artifacts import create_artifact
from orchestra.llm.usage import LLMUsage
from orchestra.prompts.agent_request import render_agent_request_messages
from orchestra.schemas.artifacts import RepositoryChangeArtifact
from orchestra.workspaces.base import WorkspaceRef
from orchestra.workspaces.git_workspace import SharedSubtaskGitWorkspaceManager

CodexClientFactory = Callable[[], Any]


def _build_codex_config(*, workspace_path: str | None = None) -> Any:
    """Build CodexConfig from env (OPENAI_BASE_URL → openai_base_url override)."""
    from openai_codex import CodexConfig

    overrides: list[str] = []
    base = (os.getenv("OPENAI_BASE_URL") or "").strip().rstrip("/")
    if base:
        # User-level openai_base_url; config_overrides also work for SDK launches.
        overrides.append(f'openai_base_url="{base}"')
    if workspace_path:
        # Mark AdaMAS workspace trusted for this process (fixture/smoke only).
        overrides.append(f'projects."{workspace_path}".trust_level="trusted"')
    return CodexConfig(config_overrides=tuple(overrides))


def _default_client_factory(*, workspace_path: str | None = None) -> Any:
    from openai_codex import AsyncCodex

    return AsyncCodex(config=_build_codex_config(workspace_path=workspace_path))


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
        # openai_codex exposes thread_resume/thread_fork with cwd rebinding.
        # Fast Loop still defaults to fresh_only unless hybrid mode is enabled.
        resume_fork = True
        try:
            from openai_codex import AsyncCodex

            resume_fork = sdk_supports_resume_fork(AsyncCodex)
        except Exception:  # noqa: BLE001
            resume_fork = False
        policies = {SessionPolicy.FRESH}
        if resume_fork:
            policies |= {SessionPolicy.RESUME, SessionPolicy.FORK}
        return BackendCapabilities(
            multi_step=True,
            code_actions=True,
            structured_tools=False,
            repository_editing=True,
            supports_remote_executor=False,
            supports_step_trace=False,
            supports_resume=resume_fork,
            supports_fork=resume_fork,
            supports_session_state=True,
            supported_session_policies=frozenset(policies),
            supports_tool_policy_edit=False,
            supports_model_override=True,
            supports_workspace_rebinding=True,
            supports_cross_workspace_resume=resume_fork,
            supports_cross_workspace_fork=resume_fork,
            supports_parallel_instances=True,
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

            async with AsyncCodex(config=_build_codex_config()) as client:
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
            status, failure_class = map_codex_exception(exc)
            return BackendHealth(
                healthy=False,
                backend_id=self.backend_id,
                detail=(
                    f"Codex runtime/auth healthcheck failed "
                    f"[{failure_class.value}/{status.value}]: "
                    f"{type(exc).__name__}: {exc}"
                ),
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

    async def _ensure_auth(self, client: Any) -> None:
        """Login with OPENAI_API_KEY when present (ChatGPT login is out of band)."""
        api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
        if not api_key:
            return
        login = getattr(client, "login_api_key", None)
        if callable(login):
            result = login(api_key)
            if asyncio.iscoroutine(result) or isinstance(result, Awaitable):
                await result  # type: ignore[misc]

    async def run(
        self,
        request: AgentRequest,
        context: BackendExecutionContext,
    ) -> AgentResult:
        started = time.perf_counter()
        # Graph YAML thread_policy remains fresh-only; runtime lifecycle is driven
        # by AgentRequest.session_policy from node session directives.
        yaml_thread = str(request.backend_config.get("thread_policy") or "fresh")
        if yaml_thread != "fresh":
            return self._fail(
                request,
                AgentRunStatus.INVALID_REQUEST,
                "CodexSDKBackendConfig.thread_policy must remain fresh; "
                "use node session directives for RESUME/FORK",
            )
        if not context.workspace_ref:
            return self._fail(
                request,
                AgentRunStatus.INVALID_REQUEST,
                "codex_sdk requires workspace_ref; refusing implicit cwd/project-root fallback",
            )
        # Fail closed on session policy before mutating/reading the workspace.
        if request.session_policy is AgentSessionPolicy.FRESH:
            if request.session_ref is not None:
                return self._fail(
                    request,
                    AgentRunStatus.INVALID_REQUEST,
                    "FRESH must not carry session_ref (no fake resume/fork)",
                )
        elif request.session_ref is None or not request.session_ref.session_id:
            return self._fail(
                request,
                AgentRunStatus.INVALID_REQUEST,
                f"{request.session_policy.value} requires parent session_ref; "
                "refusing silent FRESH downgrade",
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
                metadata={
                    "workspace_ref": workspace.path,
                    "codex_failure_class": "infra",
                },
            )

        sandbox_name = str(request.backend_config.get("sandbox") or "workspace_write")
        # Escape hatch when host cannot run Codex workspace-write sandbox (bwrap/userns).
        # YAML still forbids full_access; override is explicit and opt-in.
        sandbox_override = (os.getenv("ADAMAS_CODEX_SANDBOX_OVERRIDE") or "").strip()
        if sandbox_override:
            sandbox_name = sandbox_override
        # AdaMAS YAML key is approval_policy; openai-codex 0.1.0b3 kwarg is approval_mode.
        approval_name = str(request.backend_config.get("approval_policy") or "never")
        require_diff = bool(request.backend_config.get("require_git_diff", True))
        # Full contract messages (system + user), not only the last user turn.
        prompt = render_agent_request_messages(request)

        try:
            sandbox = map_sandbox(sandbox_name)
            approval = map_approval_policy(approval_name)
        except Exception as exc:  # noqa: BLE001
            return self._fail(
                request,
                AgentRunStatus.INVALID_REQUEST,
                str(exc),
                latency_ms=int((time.perf_counter() - started) * 1000),
                metadata={"codex_failure_class": "invalid"},
            )

        if self._client_factory is None:
            try:
                import openai_codex  # noqa: F401
            except ImportError as exc:
                return self._fail(
                    request,
                    AgentRunStatus.BACKEND_UNAVAILABLE,
                    f"openai_codex not installed: {exc}",
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    metadata={"codex_failure_class": "infra"},
                )

            def client_factory() -> Any:
                return _default_client_factory(workspace_path=workspace.path)
        else:
            client_factory = self._client_factory

        timeout = float(request.timeout_seconds or 300.0)
        thread_id = ""
        parent_thread_id: str | None = None
        final_response = ""
        usage = LLMUsage()
        parent_from_request = (
            request.session_ref.session_id if request.session_ref is not None else None
        )
        try:
            async with asyncio.timeout(timeout):
                client = client_factory()
                close = getattr(client, "close", None)
                try:
                    if hasattr(client, "__aenter__"):
                        client = await client.__aenter__()
                    await self._ensure_auth(client)
                    lifecycle = CodexThreadLifecycleAdapter(client)
                    handle = await lifecycle.open(
                        policy=request.session_policy,
                        parent_thread_id=parent_from_request,
                        cwd=workspace.path,
                        sandbox=sandbox,
                        approval_mode=approval,
                        model=request.model.name if request.model else None,
                    )
                    thread_id = handle.thread_id
                    parent_thread_id = handle.parent_thread_id
                    turn = await handle.thread.run(
                        prompt,
                        sandbox=sandbox,
                        approval_mode=approval,
                    )
                    final_response = str(getattr(turn, "final_response", "") or "")
                    turn_usage = getattr(turn, "usage", None)
                    if turn_usage is not None:
                        # TokenUsage / breakdown shapes vary by SDK build.
                        last = getattr(turn_usage, "last", None) or turn_usage
                        total = getattr(turn_usage, "total", None) or last
                        usage = LLMUsage(
                            prompt_tokens=int(
                                getattr(total, "input_tokens", 0)
                                or getattr(total, "prompt_tokens", 0)
                                or 0
                            ),
                            completion_tokens=int(
                                getattr(total, "output_tokens", 0)
                                or getattr(total, "completion_tokens", 0)
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
        except CodexLifecycleError as exc:
            return self._fail(
                request,
                AgentRunStatus.INVALID_REQUEST,
                str(exc),
                latency_ms=int((time.perf_counter() - started) * 1000),
                metadata={
                    "workspace_ref": workspace.path,
                    "codex_failure_class": "invalid",
                    "session_policy": request.session_policy.value,
                },
            )
        except TimeoutError:
            return self._fail(
                request,
                AgentRunStatus.TIMEOUT,
                f"Codex exceeded timeout_seconds={timeout}",
                latency_ms=int((time.perf_counter() - started) * 1000),
                metadata={
                    "workspace_ref": workspace.path,
                    "codex_failure_class": "infra",
                },
            )
        except Exception as exc:  # noqa: BLE001
            status, failure_class = map_codex_exception(exc)
            return self._fail(
                request,
                status,
                f"{type(exc).__name__}: {exc}",
                latency_ms=int((time.perf_counter() - started) * 1000),
                metadata={
                    "workspace_ref": workspace.path,
                    "thread_id": thread_id,
                    "codex_failure_class": failure_class.value,
                },
            )

        try:
            snap = await self._workspaces.snapshot(workspace)
        except Exception as exc:  # noqa: BLE001
            return self._fail(
                request,
                AgentRunStatus.INFRA_ERROR,
                f"post-edit git snapshot failed: {type(exc).__name__}: {exc}",
                latency_ms=int((time.perf_counter() - started) * 1000),
                metadata={"codex_failure_class": "infra"},
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
                    "codex_failure_class": "invalid",
                },
            )

        expected_schema = request.output_contract.output_schema
        if expected_schema != "RepositoryChangeArtifact":
            return self._fail(
                request,
                AgentRunStatus.OUTPUT_CONTRACT_FAILURE,
                f"codex_sdk produces RepositoryChangeArtifact, got contract {expected_schema}",
                latency_ms=int((time.perf_counter() - started) * 1000),
                metadata={"codex_failure_class": "invalid"},
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
            parent_session_id=parent_thread_id,
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
                "parent_thread_id": parent_thread_id,
                "session_policy": request.session_policy.value,
                "changed_files": list(snap.changed_files),
                "base_revision": workspace.base_revision,
                "approval_mode": "deny_all" if approval_name == "never" else approval_name,
                "sdk_approval_kwarg": "approval_mode",
                "sandbox": sandbox_name,
                "sandbox_override": sandbox_override or None,
            },
        )
