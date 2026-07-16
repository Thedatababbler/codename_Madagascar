"""Workspace manager protocol and shared types."""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field


class WorkspaceRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace_id: str
    path: str
    kind: str = "SHARED_SUBTASK_WORKSPACE"
    task_id: str
    subtask_id: str
    base_revision: str | None = None


class WorkspaceSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace_ref: str
    base_revision: str | None = None
    head_revision: str | None = None
    dirty: bool = False
    changed_files: list[str] = Field(default_factory=list)
    patch: str = ""


class WorkspaceManager(Protocol):
    async def prepare(
        self,
        *,
        source_repo: str,
        run_dir: str,
        task_id: str,
        subtask_id: str,
    ) -> WorkspaceRef: ...

    async def snapshot(self, workspace: WorkspaceRef) -> WorkspaceSnapshot: ...

    async def cleanup(self, workspace: WorkspaceRef) -> None: ...
