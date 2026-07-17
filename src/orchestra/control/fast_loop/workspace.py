"""Candidate workspace isolation and atomic winner commit."""

from __future__ import annotations

import asyncio
import hashlib
import shutil
import subprocess
from pathlib import Path
from typing import Protocol

from orchestra.workspaces.base import WorkspaceRef


class CandidateWorkspaceError(RuntimeError):
    pass


class CandidateWorkspaceManager(Protocol):
    async def prepare_base_snapshot(
        self,
        *,
        source_repo: str,
        run_dir: str,
        task_id: str,
        subtask_id: str,
    ) -> WorkspaceRef: ...

    async def fork_candidate_workspace(
        self,
        *,
        base: WorkspaceRef,
        run_dir: str,
        task_id: str,
        subtask_id: str,
        candidate_id: str,
    ) -> WorkspaceRef: ...

    async def collect_patch(self, workspace: WorkspaceRef) -> tuple[str, list[str], str]:
        """Return (patch, changed_files, patch_hash)."""
        ...

    async def commit_winner(
        self,
        *,
        base: WorkspaceRef,
        winner: WorkspaceRef,
        expected_base_revision: str | None,
    ) -> WorkspaceRef: ...

    async def discard_candidate(self, workspace: WorkspaceRef) -> None: ...


class GitCandidateWorkspaceManager:
    """Isolate writable candidates under subtasks/<id>/candidates/<cand>/repo."""

    kind = "CANDIDATE_WORKSPACE"

    def _base_path(self, run_dir: str, task_id: str, subtask_id: str) -> Path:
        return (
            Path(run_dir) / "tasks" / task_id / "subtasks" / subtask_id / "base" / "repo"
        )

    def _candidate_path(
        self,
        run_dir: str,
        task_id: str,
        subtask_id: str,
        candidate_id: str,
    ) -> Path:
        return (
            Path(run_dir)
            / "tasks"
            / task_id
            / "subtasks"
            / subtask_id
            / "candidates"
            / candidate_id
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

    def _ensure_git_repo(self, dest: Path, source: Path) -> None:
        if dest.exists():
            shutil.rmtree(dest)
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
                        "fixture baseline",
                    ],
                    cwd=dest,
                    check=True,
                    capture_output=True,
                    text=True,
                )

    def _changed_files(self, repo: Path) -> list[str]:
        status = self._run_git(repo, "status", "--porcelain")
        files: list[str] = []
        for line in status.stdout.splitlines():
            path = line[3:].strip() if len(line) >= 4 else line.strip()
            if " -> " in path:
                path = path.split(" -> ", 1)[1]
            if path and path not in files:
                files.append(path)
        return files

    async def prepare_base_snapshot(
        self,
        *,
        source_repo: str,
        run_dir: str,
        task_id: str,
        subtask_id: str,
    ) -> WorkspaceRef:
        source = Path(source_repo).resolve()
        if not source.exists():
            raise FileNotFoundError(f"source repo not found: {source}")
        dest = self._base_path(run_dir, task_id, subtask_id)

        def _prep() -> WorkspaceRef:
            if not dest.exists():
                self._ensure_git_repo(dest, source)
            status = self._run_git(dest, "status", "--porcelain")
            if status.returncode != 0:
                raise CandidateWorkspaceError(
                    f"git status failed on base: {status.stderr}"
                )
            if status.stdout.strip():
                raise CandidateWorkspaceError(
                    f"base workspace is dirty (must remain immutable): {dest}"
                )
            rev = self._rev_parse(dest)
            return WorkspaceRef(
                workspace_id=f"{task_id}/{subtask_id}/base",
                path=str(dest.resolve()),
                kind=self.kind,
                task_id=task_id,
                subtask_id=subtask_id,
                base_revision=rev,
            )

        return await asyncio.to_thread(_prep)

    async def fork_candidate_workspace(
        self,
        *,
        base: WorkspaceRef,
        run_dir: str,
        task_id: str,
        subtask_id: str,
        candidate_id: str,
    ) -> WorkspaceRef:
        dest = self._candidate_path(run_dir, task_id, subtask_id, candidate_id)
        base_path = Path(base.path)

        def _fork() -> WorkspaceRef:
            if (base_path / ".git").exists():
                if dest.exists():
                    shutil.rmtree(dest)
                dest.parent.mkdir(parents=True, exist_ok=True)
                self._run_git(base_path, "worktree", "prune")
                branch = f"candidate/{candidate_id}"
                self._run_git(base_path, "branch", "-D", branch)
                wt = self._run_git(
                    base_path,
                    "worktree",
                    "add",
                    "-b",
                    branch,
                    str(dest),
                    "HEAD",
                )
                if wt.returncode != 0:
                    self._ensure_git_repo(dest, base_path)
            else:
                self._ensure_git_repo(dest, base_path)
            return WorkspaceRef(
                workspace_id=f"{task_id}/{subtask_id}/candidates/{candidate_id}",
                path=str(dest.resolve()),
                kind=self.kind,
                task_id=task_id,
                subtask_id=subtask_id,
                base_revision=base.base_revision or self._rev_parse(dest),
            )

        return await asyncio.to_thread(_fork)

    async def collect_patch(
        self, workspace: WorkspaceRef
    ) -> tuple[str, list[str], str]:
        repo = Path(workspace.path)

        def _collect() -> tuple[str, list[str], str]:
            changed = self._changed_files(repo)
            diff = self._run_git(repo, "diff", "--binary", "--no-ext-diff", "HEAD")
            patch = diff.stdout if diff.returncode == 0 else ""
            untracked = self._run_git(
                repo, "ls-files", "--others", "--exclude-standard"
            )
            if untracked.returncode == 0:
                for rel in untracked.stdout.splitlines():
                    if rel and rel not in changed:
                        changed.append(rel)
                    show = self._run_git(
                        repo,
                        "diff",
                        "--binary",
                        "--no-ext-diff",
                        "--no-index",
                        "--",
                        "/dev/null",
                        rel,
                    )
                    if show.stdout:
                        patch = f"{patch}{show.stdout}"
            digest = hashlib.sha256(patch.encode()).hexdigest() if patch else ""
            return patch, changed, digest

        return await asyncio.to_thread(_collect)

    async def commit_winner(
        self,
        *,
        base: WorkspaceRef,
        winner: WorkspaceRef,
        expected_base_revision: str | None,
    ) -> WorkspaceRef:
        base_path = Path(base.path)
        winner_path = Path(winner.path)

        def _commit() -> WorkspaceRef:
            current = self._rev_parse(base_path)
            if expected_base_revision and current != expected_base_revision:
                raise CandidateWorkspaceError(
                    f"base revision drifted: expected {expected_base_revision}, "
                    f"got {current}"
                )
            status = self._run_git(base_path, "status", "--porcelain")
            if status.stdout.strip():
                raise CandidateWorkspaceError(
                    "base workspace dirty before winner commit; fail closed"
                )

            files = self._changed_files(winner_path)
            untracked = self._run_git(
                winner_path, "ls-files", "--others", "--exclude-standard"
            )
            if untracked.returncode == 0:
                for rel in untracked.stdout.splitlines():
                    if rel and rel not in files:
                        files.append(rel)

            diff = self._run_git(
                winner_path, "diff", "--binary", "--no-ext-diff", "HEAD"
            )
            patch_text = diff.stdout if diff.returncode == 0 else ""

            if not files and not patch_text.strip():
                return base

            applied = False
            if patch_text.strip():
                apply = subprocess.run(
                    ["git", "apply", "--whitespace=nowarn", "-"],
                    cwd=base_path,
                    input=patch_text,
                    check=False,
                    capture_output=True,
                    text=True,
                )
                applied = apply.returncode == 0
            if not applied:
                for rel in files:
                    src = winner_path / rel
                    dst = base_path / rel
                    if src.is_file():
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src, dst)
                    elif not src.exists() and dst.exists():
                        dst.unlink()

            self._run_git(base_path, "add", "-A")
            commit = subprocess.run(
                [
                    "git",
                    "-c",
                    "user.email=adamas@local",
                    "-c",
                    "user.name=AdaMAS",
                    "commit",
                    "-m",
                    f"commit winner from {winner.workspace_id}",
                ],
                cwd=base_path,
                check=False,
                capture_output=True,
                text=True,
            )
            combined = commit.stdout + commit.stderr
            if commit.returncode != 0 and "nothing to commit" not in combined:
                raise CandidateWorkspaceError(
                    f"failed to commit winner onto base: {commit.stderr}"
                )
            return WorkspaceRef(
                workspace_id=base.workspace_id,
                path=str(base_path.resolve()),
                kind=base.kind,
                task_id=base.task_id,
                subtask_id=base.subtask_id,
                base_revision=self._rev_parse(base_path),
            )

        return await asyncio.to_thread(_commit)

    async def discard_candidate(self, workspace: WorkspaceRef) -> None:
        path = Path(workspace.path)

        def _discard() -> None:
            if path.exists() and path.name == "repo":
                marker = path.parent / "DISCARDED"
                marker.write_text("discarded\n", encoding="utf-8")

        await asyncio.to_thread(_discard)
