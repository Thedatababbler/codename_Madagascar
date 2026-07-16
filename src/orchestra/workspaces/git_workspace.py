"""Shared per-subtask Git workspace manager (Milestone 3.5)."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

from orchestra.workspaces.base import WorkspaceRef, WorkspaceSnapshot


class SharedSubtaskGitWorkspaceManager:
    """Copy a source Git repo into an isolated per-subtask workspace.

    Layout: ``<run_dir>/tasks/<task_id>/workspaces/<subtask_id>/repo/``
    """

    kind = "SHARED_SUBTASK_WORKSPACE"

    def _repo_path(self, run_dir: str, task_id: str, subtask_id: str) -> Path:
        return Path(run_dir) / "tasks" / task_id / "workspaces" / subtask_id / "repo"

    def _run_git(self, cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
        )

    def _ensure_clean(self, repo: Path) -> None:
        status = self._run_git(repo, "status", "--porcelain")
        if status.returncode != 0:
            raise RuntimeError(
                f"git status failed in workspace {repo}: {status.stderr.strip()}"
            )
        if status.stdout.strip():
            raise RuntimeError(
                f"workspace is dirty before Codex execution: {repo}\n{status.stdout}"
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
        subtask_id: str,
    ) -> WorkspaceRef:
        source = Path(source_repo).resolve()
        if not source.exists():
            raise FileNotFoundError(f"source repo not found: {source}")
        dest = self._repo_path(run_dir, task_id, subtask_id)

        def _prepare_sync() -> WorkspaceRef:
            if dest.exists():
                self._ensure_clean(dest)
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                # Local clone preserves Git history without mutating the source.
                clone = subprocess.run(
                    ["git", "clone", "--local", str(source), str(dest)],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                if clone.returncode != 0:
                    # Fallback for non-bare / incomplete fixtures: copy then ensure git.
                    if dest.exists():
                        shutil.rmtree(dest)
                    shutil.copytree(source, dest, symlinks=False)
                    if not (dest / ".git").exists():
                        init = subprocess.run(
                            ["git", "init"],
                            cwd=dest,
                            check=False,
                            capture_output=True,
                            text=True,
                        )
                        if init.returncode != 0:
                            raise RuntimeError(
                                f"failed to initialize workspace git repo: {init.stderr}"
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
                self._ensure_clean(dest)

            base_revision = self._rev_parse(dest)
            workspace_id = f"{task_id}/{subtask_id}"
            return WorkspaceRef(
                workspace_id=workspace_id,
                path=str(dest.resolve()),
                kind=self.kind,
                task_id=task_id,
                subtask_id=subtask_id,
                base_revision=base_revision,
            )

        return await asyncio.to_thread(_prepare_sync)

    async def snapshot(self, workspace: WorkspaceRef) -> WorkspaceSnapshot:
        repo = Path(workspace.path)

        def _snapshot_sync() -> WorkspaceSnapshot:
            status = self._run_git(repo, "status", "--porcelain")
            if status.returncode != 0:
                raise RuntimeError(f"git status failed: {status.stderr.strip()}")
            changed: list[str] = []
            for line in status.stdout.splitlines():
                path = line[3:].strip() if len(line) >= 4 else line.strip()
                if " -> " in path:
                    path = path.split(" -> ", 1)[1]
                if path:
                    changed.append(path)
            diff = self._run_git(
                repo, "diff", "--binary", "--no-ext-diff", "HEAD"
            )
            # Include untracked as patch additions when present.
            untracked = self._run_git(
                repo, "ls-files", "--others", "--exclude-standard"
            )
            patch = diff.stdout if diff.returncode == 0 else ""
            if untracked.returncode == 0 and untracked.stdout.strip():
                for rel in untracked.stdout.splitlines():
                    if rel and rel not in changed:
                        changed.append(rel)
                    file_path = repo / rel
                    if file_path.is_file():
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
                        # git diff --no-index returns 1 when files differ.
                        if show.stdout:
                            patch = f"{patch}{show.stdout}"
            head = self._rev_parse(repo)
            return WorkspaceSnapshot(
                workspace_ref=workspace.path,
                base_revision=workspace.base_revision,
                head_revision=head,
                dirty=bool(changed) or bool(patch.strip()),
                changed_files=changed,
                patch=patch,
            )

        return await asyncio.to_thread(_snapshot_sync)

    async def cleanup(self, workspace: WorkspaceRef) -> None:
        path = Path(workspace.path)

        def _cleanup_sync() -> None:
            root = path.parent  # .../workspaces/<subtask_id>
            if root.exists():
                shutil.rmtree(root)

        await asyncio.to_thread(_cleanup_sync)
