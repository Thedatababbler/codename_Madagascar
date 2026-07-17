"""Task-level canonical repository workspace for upstream→downstream propagation."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

from orchestra.control.fast_loop.schemas import WorkspaceChangeSet
from orchestra.control.fast_loop.workspace import (
    GitCandidateWorkspaceManager,
)
from orchestra.workspaces.base import WorkspaceRef


class CanonicalTaskWorkspaceManager:
    """One mutable canonical repo per task; subtasks fork from its HEAD."""

    kind = "CANONICAL_TASK_WORKSPACE"

    def __init__(
        self, candidate_manager: GitCandidateWorkspaceManager | None = None
    ) -> None:
        self._candidates = candidate_manager or GitCandidateWorkspaceManager()

    def _path(self, run_dir: str, task_id: str) -> Path:
        return Path(run_dir) / "tasks" / task_id / "canonical" / "repo"

    def _run_git(self, cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
        )

    def _rev_parse(self, repo: Path) -> str | None:
        result = self._run_git(repo, "rev-parse", "HEAD")
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None

    async def prepare(
        self,
        *,
        source_repo: str,
        run_dir: str,
        task_id: str,
    ) -> WorkspaceRef:
        source = Path(source_repo).resolve()
        dest = self._path(run_dir, task_id)

        def _prep() -> WorkspaceRef:
            if not dest.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                clone = subprocess.run(
                    ["git", "clone", "--local", str(source), str(dest)],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                if clone.returncode != 0:
                    if dest.exists():
                        shutil.rmtree(dest)
                    shutil.copytree(source, dest, symlinks=False)
                    if not (dest / ".git").exists():
                        subprocess.run(
                            ["git", "init"],
                            cwd=dest,
                            check=True,
                            capture_output=True,
                            text=True,
                        )
                        subprocess.run(
                            ["git", "add", "-A"],
                            cwd=dest,
                            check=True,
                            capture_output=True,
                            text=True,
                        )
                        subprocess.run(
                            [
                                "git",
                                "-c",
                                "user.email=adamas@local",
                                "-c",
                                "user.name=AdaMAS",
                                "commit",
                                "-m",
                                "canonical baseline",
                            ],
                            cwd=dest,
                            check=True,
                            capture_output=True,
                            text=True,
                        )
            rev = self._rev_parse(dest)
            return WorkspaceRef(
                workspace_id=f"{task_id}/canonical",
                path=str(dest.resolve()),
                kind=self.kind,
                task_id=task_id,
                subtask_id="__canonical__",
                base_revision=rev,
            )

        return await asyncio.to_thread(_prep)

    async def fork_subtask_workspace(
        self,
        *,
        canonical: WorkspaceRef,
        run_dir: str,
        task_id: str,
        subtask_id: str,
    ) -> WorkspaceRef:
        """Create a clean subtask workspace from the current canonical revision."""
        dest = (
            Path(run_dir)
            / "tasks"
            / task_id
            / "workspaces"
            / subtask_id
            / "repo"
        )
        src = Path(canonical.path)

        def _fork() -> WorkspaceRef:
            if dest.exists():
                shutil.rmtree(dest)
            dest.parent.mkdir(parents=True, exist_ok=True)
            clone = subprocess.run(
                ["git", "clone", "--local", str(src), str(dest)],
                check=False,
                capture_output=True,
                text=True,
            )
            if clone.returncode != 0:
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(src, dest, symlinks=False)
            rev = self._rev_parse(dest)
            return WorkspaceRef(
                workspace_id=f"{task_id}/{subtask_id}",
                path=str(dest.resolve()),
                kind="SHARED_SUBTASK_WORKSPACE",
                task_id=task_id,
                subtask_id=subtask_id,
                base_revision=rev or canonical.base_revision,
            )

        return await asyncio.to_thread(_fork)

    async def apply_committed_changeset(
        self,
        *,
        canonical: WorkspaceRef,
        winner_workspace: WorkspaceRef,
        change_set: WorkspaceChangeSet,
        expected_revision: str | None,
        commit_message: str,
    ) -> WorkspaceRef:
        """Apply a committed subtask winner onto the task canonical workspace."""
        await self._candidates.apply_changeset(
            base=canonical,
            winner=winner_workspace,
            change_set=change_set,
            expected_base_revision=expected_revision,
        )
        new_rev = await self._candidates.finalize_git_commit(canonical, commit_message)
        return WorkspaceRef(
            workspace_id=canonical.workspace_id,
            path=canonical.path,
            kind=canonical.kind,
            task_id=canonical.task_id,
            subtask_id=canonical.subtask_id,
            base_revision=new_rev,
        )

    async def rollback(
        self, canonical: WorkspaceRef, revision: str
    ) -> None:
        await self._candidates.rollback_to_revision(canonical, revision)
