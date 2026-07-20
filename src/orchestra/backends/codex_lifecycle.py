"""Codex thread lifecycle adapter (FRESH / RESUME / FORK).

Uses only real openai_codex AsyncCodex APIs. Never fabricates parent IDs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from orchestra.backends.base import AgentSessionPolicy


class CodexLifecycleError(RuntimeError):
    """Raised when a lifecycle operation is unsupported or unsafe."""


@dataclass(frozen=True)
class CodexThreadHandle:
    thread_id: str
    parent_thread_id: str | None
    thread: Any
    policy: AgentSessionPolicy


class CodexThreadClient(Protocol):
    async def thread_start(self, **kwargs: Any) -> Any: ...

    async def thread_resume(self, thread_id: str, **kwargs: Any) -> Any: ...

    async def thread_fork(self, thread_id: str, **kwargs: Any) -> Any: ...


def sdk_supports_resume_fork(client: Any) -> bool:
    return callable(getattr(client, "thread_resume", None)) and callable(
        getattr(client, "thread_fork", None)
    )


class CodexThreadLifecycleAdapter:
    """Thin adapter over AsyncCodex thread_start/resume/fork."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def assert_supported(self, policy: AgentSessionPolicy) -> None:
        if policy is AgentSessionPolicy.FRESH:
            if not callable(getattr(self._client, "thread_start", None)):
                raise CodexLifecycleError("Codex SDK missing thread_start")
            return
        if not sdk_supports_resume_fork(self._client):
            raise CodexLifecycleError(
                f"Codex SDK missing real thread_resume/thread_fork for {policy.value}"
            )

    async def start_fresh(
        self,
        *,
        cwd: str,
        sandbox: Any,
        approval_mode: Any,
        model: str | None,
    ) -> CodexThreadHandle:
        self.assert_supported(AgentSessionPolicy.FRESH)
        thread = await self._client.thread_start(
            cwd=cwd,
            sandbox=sandbox,
            approval_mode=approval_mode,
            model=model,
        )
        thread_id = str(getattr(thread, "id", "") or "")
        if not thread_id:
            raise CodexLifecycleError("thread_start returned empty thread id")
        return CodexThreadHandle(
            thread_id=thread_id,
            parent_thread_id=None,
            thread=thread,
            policy=AgentSessionPolicy.FRESH,
        )

    async def resume(
        self,
        *,
        parent_thread_id: str,
        cwd: str,
        sandbox: Any,
        approval_mode: Any,
        model: str | None,
    ) -> CodexThreadHandle:
        self.assert_supported(AgentSessionPolicy.RESUME)
        if not parent_thread_id:
            raise CodexLifecycleError("RESUME requires parent_thread_id")
        thread = await self._client.thread_resume(
            parent_thread_id,
            cwd=cwd,
            sandbox=sandbox,
            approval_mode=approval_mode,
            model=model,
        )
        thread_id = str(getattr(thread, "id", "") or "")
        if not thread_id:
            raise CodexLifecycleError("thread_resume returned empty thread id")
        # SDK may keep the same id or return a resumed identity; parent is explicit.
        return CodexThreadHandle(
            thread_id=thread_id,
            parent_thread_id=parent_thread_id,
            thread=thread,
            policy=AgentSessionPolicy.RESUME,
        )

    async def fork(
        self,
        *,
        parent_thread_id: str,
        cwd: str,
        sandbox: Any,
        approval_mode: Any,
        model: str | None,
    ) -> CodexThreadHandle:
        self.assert_supported(AgentSessionPolicy.FORK)
        if not parent_thread_id:
            raise CodexLifecycleError("FORK requires parent_thread_id")
        thread = await self._client.thread_fork(
            parent_thread_id,
            cwd=cwd,
            sandbox=sandbox,
            approval_mode=approval_mode,
            model=model,
        )
        thread_id = str(getattr(thread, "id", "") or "")
        if not thread_id:
            raise CodexLifecycleError("thread_fork returned empty thread id")
        if thread_id == parent_thread_id:
            raise CodexLifecycleError(
                "fake FORK forbidden: child thread id equals parent thread id"
            )
        return CodexThreadHandle(
            thread_id=thread_id,
            parent_thread_id=parent_thread_id,
            thread=thread,
            policy=AgentSessionPolicy.FORK,
        )

    async def open(
        self,
        *,
        policy: AgentSessionPolicy,
        parent_thread_id: str | None,
        cwd: str,
        sandbox: Any,
        approval_mode: Any,
        model: str | None,
    ) -> CodexThreadHandle:
        if policy is AgentSessionPolicy.FRESH:
            if parent_thread_id:
                raise CodexLifecycleError(
                    "FRESH must not carry a parent_thread_id (no fake fork/resume)"
                )
            return await self.start_fresh(
                cwd=cwd,
                sandbox=sandbox,
                approval_mode=approval_mode,
                model=model,
            )
        if policy is AgentSessionPolicy.RESUME:
            return await self.resume(
                parent_thread_id=str(parent_thread_id or ""),
                cwd=cwd,
                sandbox=sandbox,
                approval_mode=approval_mode,
                model=model,
            )
        if policy is AgentSessionPolicy.FORK:
            return await self.fork(
                parent_thread_id=str(parent_thread_id or ""),
                cwd=cwd,
                sandbox=sandbox,
                approval_mode=approval_mode,
                model=model,
            )
        raise CodexLifecycleError(f"unsupported session policy: {policy}")
