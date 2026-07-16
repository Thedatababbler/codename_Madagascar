"""Codex SDK type aliases and helpers (Milestone 3.5)."""

from __future__ import annotations

from typing import Any, Protocol


class CodexTurnResultLike(Protocol):
    final_response: str | None
    usage: Any


class CodexThreadLike(Protocol):
    id: str

    async def run(self, prompt: str, **kwargs: Any) -> CodexTurnResultLike: ...


class CodexClientLike(Protocol):
    async def thread_start(self, **kwargs: Any) -> CodexThreadLike: ...

    async def close(self) -> None: ...

    async def __aenter__(self) -> CodexClientLike: ...

    async def __aexit__(self, exc_type, exc, tb) -> None: ...  # noqa: ANN001


def map_approval_policy(policy: str) -> Any:
    """Map AdaMAS approval_policy to openai_codex.ApprovalMode."""
    from openai_codex import ApprovalMode

    if policy == "never":
        return ApprovalMode.deny_all
    raise ValueError(f"unsupported approval_policy: {policy}")


def map_sandbox(sandbox: str) -> Any:
    from openai_codex import Sandbox

    if sandbox == "read_only":
        return Sandbox.read_only
    if sandbox == "workspace_write":
        return Sandbox.workspace_write
    raise ValueError(f"unsupported sandbox: {sandbox}")
