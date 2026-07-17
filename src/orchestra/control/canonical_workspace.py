"""Task-level canonical repository workspace for upstream→downstream propagation."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import uuid
from pathlib import Path

from orchestra.control.fast_loop.schemas import WorkspaceChangeSet
from orchestra.control.fast_loop.workspace import (
    CandidateWorkspaceError,
    GitCandidateWorkspaceManager,
)
from orchestra.harness.command_runner import run_authoritative_harness_command
from orchestra.workspaces.base import WorkspaceRef


class CanonicalCommitError(RuntimeError):
    """Raised when staging apply/harness/promotion fails."""

    def __init__(
        self,
        message: str,
        *,
        conflict: bool = False,
        validation_failed: bool = False,
        conflict_files: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.conflict = conflict
        self.validation_failed = validation_failed
        self.conflict_files = conflict_files or []


class CanonicalTaskWorkspaceManager:
    """One mutable canonical repo per task; subtasks fork from its HEAD."""

    kind = "CANONICAL_TASK_WORKSPACE"

    def __init__(
        self, candidate_manager: GitCandidateWorkspaceManager | None = None
    ) -> None:
        self._candidates = candidate_manager or GitCandidateWorkspaceManager()

    def _path(self, run_dir: str, task_id: str) -> Path:
        return Path(run_dir) / "tasks" / task_id / "canonical" / "repo"

    def _staging_path(
        self, run_dir: str, task_id: str, subtask_id: str, attempt_id: int
    ) -> Path:
        token = uuid.uuid4().hex[:8]
        return (
            Path(run_dir)
            / "tasks"
            / task_id
            / "commit_staging"
            / f"{subtask_id}-{attempt_id}-{token}"
            / "repo"
        )

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

    async def create_staging(
        self,
        *,
        canonical: WorkspaceRef,
        run_dir: str,
        task_id: str,
        subtask_id: str,
        attempt_id: int,
    ) -> WorkspaceRef:
        """Clone canonical HEAD into an isolated commit staging workspace."""
        dest = self._staging_path(run_dir, task_id, subtask_id, attempt_id)
        src = Path(canonical.path)

        def _stage() -> WorkspaceRef:
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
                workspace_id=f"{task_id}/staging/{subtask_id}/{attempt_id}",
                path=str(dest.resolve()),
                kind="COMMIT_STAGING_WORKSPACE",
                task_id=task_id,
                subtask_id=subtask_id,
                base_revision=rev,
            )

        return await asyncio.to_thread(_stage)

    async def promote_staging(
        self,
        *,
        canonical: WorkspaceRef,
        staging: WorkspaceRef,
        expected_parent_revision: str | None,
    ) -> WorkspaceRef:
        """Replace canonical tree with validated staging (transactional promote)."""
        dest = Path(canonical.path)
        src = Path(staging.path)

        def _promote() -> WorkspaceRef:
            current = self._rev_parse(dest)
            if expected_parent_revision and current != expected_parent_revision:
                raise CanonicalCommitError(
                    f"canonical drifted during promote: expected "
                    f"{expected_parent_revision}, got {current}",
                    conflict=True,
                )
            parent = dest.parent
            backup = parent / f"repo.bak.{uuid.uuid4().hex[:8]}"
            if dest.exists():
                dest.rename(backup)
            try:
                parent.mkdir(parents=True, exist_ok=True)
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
                if backup.exists():
                    shutil.rmtree(backup)
            except Exception:
                if dest.exists():
                    shutil.rmtree(dest)
                if backup.exists():
                    backup.rename(dest)
                raise
            rev = self._rev_parse(dest)
            return WorkspaceRef(
                workspace_id=canonical.workspace_id,
                path=str(dest.resolve()),
                kind=canonical.kind,
                task_id=canonical.task_id,
                subtask_id=canonical.subtask_id,
                base_revision=rev,
            )

        return await asyncio.to_thread(_promote)

    async def transactional_commit(
        self,
        *,
        canonical: WorkspaceRef,
        winner_workspace: WorkspaceRef,
        change_set: WorkspaceChangeSet,
        run_dir: str,
        task_id: str,
        subtask_id: str,
        attempt_id: int,
        commit_message: str,
        harness_command: list[str],
        harness_timeout: float,
    ) -> tuple[WorkspaceRef, str | None]:
        """
        Staging apply + authoritative harness + promote.

        Canonical is untouched until promote succeeds.
        """
        parent_rev = self._rev_parse(Path(canonical.path)) or canonical.base_revision
        staging = await self.create_staging(
            canonical=canonical,
            run_dir=run_dir,
            task_id=task_id,
            subtask_id=subtask_id,
            attempt_id=attempt_id,
        )
        try:
            await self._candidates.apply_changeset(
                base=staging,
                winner=winner_workspace,
                change_set=change_set,
                expected_base_revision=None,
                allow_content_fallback=False,
                enforce_base_revision=False,
            )
        except CandidateWorkspaceError as exc:
            raise CanonicalCommitError(
                str(exc),
                conflict=True,
                conflict_files=list(change_set.modified_files),
            ) from exc

        new_rev = await self._candidates.finalize_git_commit(staging, commit_message)
        passed, code, stdout, stderr = await run_authoritative_harness_command(
            cwd=staging.path,
            command=harness_command,
            timeout_seconds=harness_timeout,
        )
        if not passed:
            raise CanonicalCommitError(
                f"canonical staging harness failed exit={code}: "
                f"{(stderr or stdout)[-500:]}",
                validation_failed=True,
            )

        promoted = await self.promote_staging(
            canonical=canonical,
            staging=staging,
            expected_parent_revision=parent_rev,
        )
        return promoted, new_rev or promoted.base_revision

    async def apply_committed_changeset(
        self,
        *,
        canonical: WorkspaceRef,
        winner_workspace: WorkspaceRef,
        change_set: WorkspaceChangeSet,
        expected_revision: str | None,
        commit_message: str,
    ) -> WorkspaceRef:
        """Legacy direct apply (tests); prefer transactional_commit."""
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
