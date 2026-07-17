"""Candidate workspace isolation and atomic winner commit."""

from __future__ import annotations

import asyncio
import hashlib
import shutil
import subprocess
from pathlib import Path
from typing import Protocol

from orchestra.control.fast_loop.schemas import RenameRecord, WorkspaceChangeSet
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

    async def collect_changeset(self, workspace: WorkspaceRef) -> WorkspaceChangeSet: ...

    async def collect_patch(self, workspace: WorkspaceRef) -> tuple[str, list[str], str]: ...

    async def apply_changeset(
        self,
        *,
        base: WorkspaceRef,
        winner: WorkspaceRef,
        change_set: WorkspaceChangeSet,
        expected_base_revision: str | None,
        allow_content_fallback: bool = True,
        enforce_base_revision: bool = True,
    ) -> None: ...

    async def collect_changeset_since(
        self, workspace: WorkspaceRef, base_revision: str
    ) -> WorkspaceChangeSet: ...

    async def rollback_to_revision(
        self, workspace: WorkspaceRef, revision: str
    ) -> None: ...

    async def commit_winner(
        self,
        *,
        base: WorkspaceRef,
        winner: WorkspaceRef,
        expected_base_revision: str | None,
        change_set: WorkspaceChangeSet | None = None,
        finalize_git_commit: bool = True,
    ) -> WorkspaceRef: ...

    async def discard_candidate(self, workspace: WorkspaceRef) -> None: ...


def _safe_relpath(root: Path, rel: str) -> Path:
    """Resolve rel under root; reject traversal and symlink escapes."""
    if not rel or rel.startswith("/") or "\x00" in rel:
        raise CandidateWorkspaceError(f"illegal path: {rel!r}")
    parts = Path(rel).parts
    if ".." in parts or parts[:1] == (".git",) or ".git" in parts:
        raise CandidateWorkspaceError(f"path escapes workspace: {rel!r}")
    root_resolved = root.resolve()
    target = (root_resolved / rel).resolve()
    try:
        target.relative_to(root_resolved)
    except ValueError as exc:
        raise CandidateWorkspaceError(f"path escapes workspace: {rel!r}") from exc
    # Reject symlink that points outside root.
    cursor = root_resolved
    for part in Path(rel).parts:
        cursor = cursor / part
        if cursor.is_symlink():
            linked = cursor.resolve()
            try:
                linked.relative_to(root_resolved)
            except ValueError as exc:
                raise CandidateWorkspaceError(
                    f"symlink escapes workspace: {rel!r}"
                ) from exc
    return target


def parse_porcelain_z(payload: bytes) -> list[tuple[str, str, str | None]]:
    """Parse ``git status --porcelain=v1 -z`` into (xy, path, rename_from?)."""
    entries: list[tuple[str, str, str | None]] = []
    if not payload:
        return entries
    parts = payload.split(b"\x00")
    i = 0
    while i < len(parts):
        item = parts[i]
        if not item:
            i += 1
            continue
        text = item.decode("utf-8", errors="replace")
        if len(text) < 4:
            i += 1
            continue
        xy = text[:2]
        path = text[3:]
        rename_from: str | None = None
        # Rename/copy: next NUL-separated field is the other path.
        if xy[0] in {"R", "C"} or xy[1] in {"R", "C"}:
            if i + 1 < len(parts) and parts[i + 1]:
                rename_from = parts[i + 1].decode("utf-8", errors="replace")
                i += 1
                # porcelain -z for rename: "XY score\0new\0old" or "R  new\0old"
                # After splitting first record as "R  newpath", next is old path.
                # Actually format is: XY <new>\0<old>\0 for renames when -z.
                # Our path already is the first path after XY.
                pass
        entries.append((xy, path, rename_from))
        i += 1
    return entries


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

    def _run_git_bytes(self, cwd: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=False,
            capture_output=True,
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

    def _build_changeset(self, repo: Path) -> WorkspaceChangeSet:
        status = self._run_git_bytes(repo, "status", "--porcelain=v1", "-z")
        entries = parse_porcelain_z(status.stdout if status.returncode == 0 else b"")
        modified: list[str] = []
        deleted: list[str] = []
        untracked: list[str] = []
        renames: list[RenameRecord] = []
        for xy, path, rename_from in entries:
            if xy == "??":
                untracked.append(path)
                continue
            if "R" in xy or "C" in xy:
                # With -z, path is typically the destination; rename_from the source.
                if rename_from:
                    renames.append(RenameRecord(from_path=rename_from, to_path=path))
                else:
                    # Fallback: path may contain "old -> new"
                    if " -> " in path:
                        src, dst = path.split(" -> ", 1)
                        renames.append(RenameRecord(from_path=src, to_path=dst))
                    else:
                        modified.append(path)
                continue
            if "D" in xy:
                deleted.append(path)
                continue
            if path:
                modified.append(path)

        # Also catch untracked via ls-files for robustness.
        others = self._run_git(repo, "ls-files", "--others", "--exclude-standard")
        if others.returncode == 0:
            for rel in others.stdout.splitlines():
                if rel and rel not in untracked:
                    untracked.append(rel)

        tracked_diff = self._run_git(repo, "diff", "--binary", "--no-ext-diff", "HEAD")
        tracked_patch = tracked_diff.stdout if tracked_diff.returncode == 0 else ""

        manifest_parts = [
            f"M:{p}" for p in sorted(set(modified))
        ] + [
            f"A:{p}" for p in sorted(set(untracked))
        ] + [
            f"D:{p}" for p in sorted(set(deleted))
        ] + [
            f"R:{r.from_path}->{r.to_path}" for r in renames
        ]
        digest = hashlib.sha256("\n".join(manifest_parts).encode()).hexdigest()
        return WorkspaceChangeSet(
            tracked_patch=tracked_patch,
            modified_files=sorted(set(modified)),
            added_untracked_files=sorted(set(untracked)),
            deleted_files=sorted(set(deleted)),
            renamed_files=renames,
            file_manifest_hash=digest,
        )

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

    async def collect_changeset(self, workspace: WorkspaceRef) -> WorkspaceChangeSet:
        repo = Path(workspace.path)
        return await asyncio.to_thread(self._build_changeset, repo)

    async def collect_patch(
        self, workspace: WorkspaceRef
    ) -> tuple[str, list[str], str]:
        cs = await self.collect_changeset(workspace)
        changed = sorted(
            set(cs.modified_files)
            | set(cs.added_untracked_files)
            | set(cs.deleted_files)
            | {r.to_path for r in cs.renamed_files}
            | {r.from_path for r in cs.renamed_files}
        )
        # Full patch text for telemetry: tracked + synthetic untracked diffs.
        repo = Path(workspace.path)
        patch = cs.tracked_patch
        for rel in cs.added_untracked_files:
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
        digest = hashlib.sha256(patch.encode()).hexdigest() if patch else cs.file_manifest_hash
        return patch, changed, digest

    def _build_changeset_since(self, repo: Path, base_revision: str) -> WorkspaceChangeSet:
        name_status = self._run_git(
            repo, "diff", "--name-status", "--no-renames", base_revision, "HEAD"
        )
        modified: list[str] = []
        deleted: list[str] = []
        added: list[str] = []
        if name_status.returncode == 0:
            for line in name_status.stdout.splitlines():
                if not line.strip():
                    continue
                parts = line.split("\t")
                if len(parts) < 2:
                    continue
                code, path = parts[0], parts[1]
                if code.startswith("A"):
                    added.append(path)
                elif code.startswith("D"):
                    deleted.append(path)
                else:
                    modified.append(path)
        tracked = self._run_git(
            repo, "diff", "--binary", "--no-ext-diff", base_revision, "HEAD"
        )
        tracked_patch = tracked.stdout if tracked.returncode == 0 else ""
        # Untracked relative to current HEAD (should be empty if committed).
        dirty = self._build_changeset(repo)
        untracked = list(dirty.added_untracked_files)
        # Files added between base..HEAD are tracked now; treat as modified/added.
        for rel in added:
            if rel not in modified:
                modified.append(rel)
        manifest_parts = (
            [f"M:{p}" for p in sorted(set(modified))]
            + [f"A:{p}" for p in sorted(set(untracked))]
            + [f"D:{p}" for p in sorted(set(deleted))]
        )
        digest = hashlib.sha256("\n".join(manifest_parts).encode()).hexdigest()
        return WorkspaceChangeSet(
            tracked_patch=tracked_patch,
            modified_files=sorted(set(modified)),
            added_untracked_files=sorted(set(untracked)),
            deleted_files=sorted(set(deleted)),
            renamed_files=[],
            file_manifest_hash=digest,
        )

    async def collect_changeset_since(
        self, workspace: WorkspaceRef, base_revision: str
    ) -> WorkspaceChangeSet:
        repo = Path(workspace.path)
        return await asyncio.to_thread(self._build_changeset_since, repo, base_revision)

    def _apply_changeset_sync(
        self,
        *,
        base_path: Path,
        winner_path: Path,
        change_set: WorkspaceChangeSet,
        expected_base_revision: str | None,
        allow_content_fallback: bool = True,
        enforce_base_revision: bool = True,
    ) -> None:
        current = self._rev_parse(base_path)
        if (
            enforce_base_revision
            and expected_base_revision
            and current != expected_base_revision
        ):
            raise CandidateWorkspaceError(
                f"base revision drifted: expected {expected_base_revision}, got {current}"
            )
        status = self._run_git(base_path, "status", "--porcelain")
        if status.stdout.strip():
            raise CandidateWorkspaceError(
                "base workspace dirty before winner apply; fail closed"
            )

        if change_set.tracked_patch.strip():
            apply = subprocess.run(
                ["git", "apply", "--whitespace=nowarn", "-"],
                cwd=base_path,
                input=change_set.tracked_patch,
                check=False,
                capture_output=True,
                text=True,
            )
            if apply.returncode != 0:
                if not allow_content_fallback:
                    # Controlled reapply after sibling commits:
                    # - identical content already present → OK (idempotent)
                    # - missing new file → safe copy from winner
                    # - existing file with different content → conflict
                    conflicts: list[str] = []
                    for rel in (
                        change_set.modified_files + change_set.added_untracked_files
                    ):
                        src = _safe_relpath(winner_path, rel)
                        dst = _safe_relpath(base_path, rel)
                        if not src.is_file():
                            continue
                        if not dst.is_file():
                            dst.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(src, dst)
                        elif src.read_bytes() != dst.read_bytes():
                            conflicts.append(rel)
                    if conflicts:
                        raise CandidateWorkspaceError(
                            "patch apply conflict on "
                            f"{conflicts}: {apply.stderr or apply.stdout}"
                        )
                else:
                    # Fallback: copy modified tracked files from winner.
                    for rel in change_set.modified_files:
                        src = _safe_relpath(winner_path, rel)
                        dst = _safe_relpath(base_path, rel)
                        if src.is_file():
                            dst.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(src, dst)

        for rename in change_set.renamed_files:
            src_old = _safe_relpath(base_path, rename.from_path)
            src_new = _safe_relpath(winner_path, rename.to_path)
            dst_new = _safe_relpath(base_path, rename.to_path)
            if src_new.is_file():
                dst_new.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_new, dst_new)
            if src_old.exists() and src_old != dst_new:
                if src_old.is_file():
                    src_old.unlink()
                elif src_old.is_dir():
                    shutil.rmtree(src_old)

        for rel in change_set.deleted_files:
            dst = _safe_relpath(base_path, rel)
            if dst.is_file():
                dst.unlink()
            elif dst.is_dir():
                shutil.rmtree(dst)

        for rel in change_set.added_untracked_files:
            src = _safe_relpath(winner_path, rel)
            dst = _safe_relpath(base_path, rel)
            if src.is_symlink():
                raise CandidateWorkspaceError(f"refusing to copy symlink: {rel}")
            if not src.is_file():
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

        # Manifest verification: every declared path must match winner content.
        for rel in change_set.modified_files + change_set.added_untracked_files:
            src = _safe_relpath(winner_path, rel)
            dst = _safe_relpath(base_path, rel)
            if src.is_file():
                if not dst.is_file():
                    raise CandidateWorkspaceError(
                        f"post-apply missing file from winner: {rel}"
                    )
                if src.read_bytes() != dst.read_bytes():
                    raise CandidateWorkspaceError(
                        f"post-apply content mismatch for {rel}"
                    )
        for rel in change_set.deleted_files:
            dst = _safe_relpath(base_path, rel)
            if dst.exists():
                raise CandidateWorkspaceError(
                    f"post-apply delete failed; still exists: {rel}"
                )

    async def apply_changeset(
        self,
        *,
        base: WorkspaceRef,
        winner: WorkspaceRef,
        change_set: WorkspaceChangeSet,
        expected_base_revision: str | None,
        allow_content_fallback: bool = True,
        enforce_base_revision: bool = True,
    ) -> None:
        await asyncio.to_thread(
            self._apply_changeset_sync,
            base_path=Path(base.path),
            winner_path=Path(winner.path),
            change_set=change_set,
            expected_base_revision=expected_base_revision,
            allow_content_fallback=allow_content_fallback,
            enforce_base_revision=enforce_base_revision,
        )

    async def rollback_to_revision(
        self, workspace: WorkspaceRef, revision: str
    ) -> None:
        repo = Path(workspace.path)

        def _rollback() -> None:
            hard = self._run_git(repo, "reset", "--hard", revision)
            if hard.returncode != 0:
                raise CandidateWorkspaceError(
                    f"rollback reset failed: {hard.stderr}"
                )
            clean = self._run_git(repo, "clean", "-fdx")
            if clean.returncode != 0:
                raise CandidateWorkspaceError(
                    f"rollback clean failed: {clean.stderr}"
                )

        await asyncio.to_thread(_rollback)

    async def finalize_git_commit(
        self, workspace: WorkspaceRef, message: str
    ) -> str | None:
        repo = Path(workspace.path)

        def _commit() -> str | None:
            self._run_git(repo, "add", "-A")
            commit = subprocess.run(
                [
                    "git",
                    "-c",
                    "user.email=adamas@local",
                    "-c",
                    "user.name=AdaMAS",
                    "commit",
                    "-m",
                    message,
                ],
                cwd=repo,
                check=False,
                capture_output=True,
                text=True,
            )
            combined = commit.stdout + commit.stderr
            if commit.returncode != 0 and "nothing to commit" not in combined:
                raise CandidateWorkspaceError(
                    f"failed to finalize git commit: {commit.stderr}"
                )
            return self._rev_parse(repo)

        return await asyncio.to_thread(_commit)

    async def commit_winner(
        self,
        *,
        base: WorkspaceRef,
        winner: WorkspaceRef,
        expected_base_revision: str | None,
        change_set: WorkspaceChangeSet | None = None,
        finalize_git_commit: bool = True,
    ) -> WorkspaceRef:
        """Apply winner changes (tracked+untracked+delete+rename) then optionally commit."""
        cs = change_set or await self.collect_changeset(winner)
        await self.apply_changeset(
            base=base,
            winner=winner,
            change_set=cs,
            expected_base_revision=expected_base_revision,
        )
        new_rev = expected_base_revision
        if finalize_git_commit:
            new_rev = await self.finalize_git_commit(
                base, f"commit winner from {winner.workspace_id}"
            )
        return WorkspaceRef(
            workspace_id=base.workspace_id,
            path=str(Path(base.path).resolve()),
            kind=base.kind,
            task_id=base.task_id,
            subtask_id=base.subtask_id,
            base_revision=new_rev or self._rev_parse(Path(base.path)),
        )

    async def discard_candidate(self, workspace: WorkspaceRef) -> None:
        path = Path(workspace.path)

        def _discard() -> None:
            if path.exists() and path.name == "repo":
                marker = path.parent / "DISCARDED"
                marker.write_text("discarded\n", encoding="utf-8")

        await asyncio.to_thread(_discard)
