"""Workspace lifecycle for repository-editing backends (Milestone 3.5)."""

from orchestra.workspaces.base import WorkspaceManager, WorkspaceRef, WorkspaceSnapshot
from orchestra.workspaces.git_workspace import SharedSubtaskGitWorkspaceManager

__all__ = [
    "SharedSubtaskGitWorkspaceManager",
    "WorkspaceManager",
    "WorkspaceRef",
    "WorkspaceSnapshot",
]
