"""Bounded repository-editing tools for smolagents (workspace-isolated)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from orchestra.tools.base import ToolBuildContext, ToolRegistry

REPOSITORY_TOOL_IDS = (
    "list_workspace_files",
    "read_workspace_file",
    "write_workspace_file",
    "apply_workspace_patch",
    "run_public_check",
    "final_answer",
)

_MAX_LIST_ENTRIES = 500
_MAX_READ_BYTES = 256_000
_MAX_WRITE_BYTES = 512_000
_MAX_PATCH_BYTES = 512_000

_LIST_SKIP_DIR_NAMES = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".adamas_private",
    "node_modules",
}

# AdaMAS assets normally live outside the workspace; these names stay reserved
# so an agent cannot fabricate something the harness might pick up.
_PROTECTED_RELATIVE_PATHS = {
    ".adamas_trusted_harness",
    "tests/test_adamas_workspace_ok.py",
    "adamas_public_harness.json",
    "adamas_milestone_contracts.json",
    "scripts/adamas_public_check.py",
    "tests_public/test_milestone_contracts.py",
    "ADAMAS_CHANGELOG.md",
    "ADAMAS_DECISIONS.md",
    "MILESTONE.md",
}

_WRITE_PROTECTED_RELATIVE_PATHS: set[str] = set()

_PUBLIC_CHECK_LEVELS = frozenset({"discovery", "implementation", "integration"})


class WorkspacePathError(ValueError):
    """Raised when a tool path is unauthorized."""


def _workspace_root(context: ToolBuildContext) -> Path:
    raw = context.workspace_ref
    if not raw or not str(raw).strip():
        raise WorkspacePathError("workspace_ref is required for repository tools")
    root = Path(str(raw)).resolve()
    if not root.is_dir():
        raise WorkspacePathError(f"workspace_ref is not a directory: {root}")
    return root


def resolve_authorized_path(
    root: Path,
    relative_path: str,
    *,
    allow_missing: bool = False,
) -> Path:
    """Resolve ``relative_path`` under ``root`` with traversal/symlink guards."""
    if not isinstance(relative_path, str) or not relative_path.strip():
        raise WorkspacePathError("path must be a non-empty relative string")
    candidate = Path(relative_path)
    if candidate.is_absolute():
        raise WorkspacePathError("absolute paths are forbidden")
    if any(part == ".." for part in candidate.parts):
        raise WorkspacePathError("path traversal ('..') is forbidden")
    if any(part == ".git" for part in candidate.parts):
        raise WorkspacePathError("access to .git is forbidden")
    # Reject null bytes / odd separators.
    if "\x00" in relative_path:
        raise WorkspacePathError("invalid path")

    # Walk components without following symlinks until the final resolve check.
    cur = root
    for part in candidate.parts:
        nxt = cur / part
        if nxt.is_symlink():
            # Resolve symlink target and ensure it stays inside the workspace.
            target = nxt.resolve()
            try:
                target.relative_to(root)
            except ValueError as exc:
                raise WorkspacePathError(
                    "symlink escapes authorized workspace"
                ) from exc
            cur = target
        else:
            cur = nxt

    resolved = cur.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise WorkspacePathError("path escapes authorized workspace") from exc

    if not allow_missing and not resolved.exists():
        raise WorkspacePathError(f"path does not exist: {relative_path}")
    return resolved


def _rel_key(relative_path: str) -> str:
    key = Path(relative_path).as_posix()
    while key.startswith("./"):
        key = key[2:]
    return key


def _is_protected(relative_path: str) -> bool:
    key = _rel_key(relative_path)
    if key in _PROTECTED_RELATIVE_PATHS:
        return True
    if key.endswith("/.adamas_trusted_harness") or key == ".adamas_trusted_harness":
        return True
    if Path(key).name == "test_adamas_workspace_ok.py":
        return True
    return False


def _is_write_protected(relative_path: str) -> bool:
    key = _rel_key(relative_path)
    return key in _WRITE_PROTECTED_RELATIVE_PATHS or _is_protected(key)


def _tool_error(exc: Exception) -> str:
    return f"ToolError: {type(exc).__name__}: {exc}"


def _build_repository_tools(context: ToolBuildContext) -> dict[str, Any]:
    from smolagents import FinalAnswerTool, tool

    root = _workspace_root(context)
    level = str(
        (context.metadata or {}).get("public_harness_level")
        or (context.metadata or {}).get("public_check_level")
        or "integration"
    )
    if level not in _PUBLIC_CHECK_LEVELS:
        level = "integration"

    @tool
    def list_workspace_files(subdirectory: str = ".") -> str:
        """List files under the authorized workspace (bounded).

        Args:
            subdirectory: Relative directory inside the workspace (default '.').
        """
        try:
            base = (
                root
                if subdirectory in {".", "", "./"}
                else resolve_authorized_path(root, subdirectory, allow_missing=False)
            )
            if not base.is_dir():
                raise WorkspacePathError("subdirectory is not a directory")
            entries: list[str] = []
            for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
                # Prune ignored directories in-place.
                dirnames[:] = [
                    d
                    for d in sorted(dirnames)
                    if d not in _LIST_SKIP_DIR_NAMES and not d.startswith(".adamas_")
                ]
                rel_dir = Path(dirpath).resolve().relative_to(root).as_posix()
                if rel_dir == ".":
                    rel_dir = ""
                for name in sorted(filenames):
                    if name in {".adamas_trusted_harness"}:
                        continue
                    rel = f"{rel_dir}/{name}" if rel_dir else name
                    if _is_protected(rel):
                        continue
                    entries.append(rel)
                    if len(entries) >= _MAX_LIST_ENTRIES:
                        return (
                            "\n".join(entries)
                            + f"\n... truncated at {_MAX_LIST_ENTRIES} entries"
                        )
            return "\n".join(entries) if entries else "(empty)"
        except Exception as exc:  # noqa: BLE001
            return _tool_error(exc)

    @tool
    def read_workspace_file(path: str) -> str:
        """Read a UTF-8 text file from the authorized workspace.

        Args:
            path: Relative file path inside the workspace.
        """
        try:
            if _is_protected(path):
                raise WorkspacePathError("reading runner-owned path is forbidden")
            target = resolve_authorized_path(root, path, allow_missing=False)
            if not target.is_file():
                raise WorkspacePathError("path is not a file")
            data = target.read_bytes()
            if len(data) > _MAX_READ_BYTES:
                raise WorkspacePathError(
                    f"file exceeds read limit of {_MAX_READ_BYTES} bytes"
                )
            return data.decode("utf-8")
        except Exception as exc:  # noqa: BLE001
            return _tool_error(exc)

    def _write_file_impl(path: str, content: str, *, limit: int) -> str:
        if _is_write_protected(path):
            raise WorkspacePathError("editing runner-owned path is forbidden")
        if not isinstance(content, str):
            raise WorkspacePathError("content must be a string")
        encoded = content.encode("utf-8")
        if len(encoded) > limit:
            raise WorkspacePathError(f"content exceeds write limit of {limit} bytes")
        target = resolve_authorized_path(root, path, allow_missing=True)
        if target.exists() and target.is_dir():
            raise WorkspacePathError("cannot write to a directory path")
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            target.parent.resolve().relative_to(root)
        except ValueError as exc:
            raise WorkspacePathError("parent path escapes workspace") from exc
        # Re-resolve after mkdir in case parents were symlinks.
        target = resolve_authorized_path(root, path, allow_missing=True)
        target.write_text(content, encoding="utf-8")
        return f"OK wrote {Path(path).as_posix()} ({len(encoded)} bytes)"

    @tool
    def write_workspace_file(path: str, content: str) -> str:
        """Write UTF-8 text to a relative path inside the authorized workspace.

        Args:
            path: Relative file path inside the workspace.
            content: Full file contents to write.
        """
        try:
            return _write_file_impl(path, content, limit=_MAX_WRITE_BYTES)
        except Exception as exc:  # noqa: BLE001
            return _tool_error(exc)

    @tool
    def apply_workspace_patch(path: str, content: str) -> str:
        """Create or replace a workspace file with the provided content (safe patch).

        This is a controlled single-file replace. Arbitrary shell patches and
        multi-file freeform diffs are rejected.

        Args:
            path: Relative file path inside the workspace.
            content: Replacement file contents.
        """
        try:
            return _write_file_impl(path, content, limit=_MAX_PATCH_BYTES)
        except Exception as exc:  # noqa: BLE001
            return _tool_error(exc)

    @tool
    def run_public_check() -> str:
        """Run the server-controlled public harness check for this milestone.

        The command is fixed by AdaMAS. The model cannot supply a shell command.
        This feedback is advisory; the scheduler reruns the authoritative check.
        """
        try:
            # Harness assets are runner-owned and live outside the workspace so
            # the agent can neither read nor rewrite them.
            script = Path(os.getenv("ADAMAS_PUBLIC_CHECK_SCRIPT", ""))
            manifest = Path(os.getenv("ADAMAS_PUBLIC_CHECK_MANIFEST", ""))
            if not script.is_file() or not manifest.is_file():
                raise WorkspacePathError("public check harness is not configured")
            command = [
                "python",
                str(script),
                "--manifest",
                str(manifest),
                "--level",
                level,
            ]
            contracts = os.getenv("ADAMAS_PUBLIC_CHECK_CONTRACTS", "")
            if contracts and Path(contracts).is_file():
                command += ["--contracts", contracts]
            env = os.environ.copy()
            # Scope untrusted-harness escape hatch to this subprocess only.
            env["ADAMAS_ALLOW_UNTRUSTED_REPO_HARNESS"] = "1"
            # Never leak secrets into the advisory check environment beyond redaction
            # already applied by the outer process; strip obvious secret keys.
            for key in list(env):
                upper = key.upper()
                if any(
                    tok in upper
                    for tok in ("API_KEY", "SECRET", "TOKEN", "PASSWORD", "CREDENTIAL")
                ):
                    env.pop(key, None)
            proc = subprocess.run(
                command,
                cwd=str(root),
                capture_output=True,
                text=True,
                timeout=120,
                env=env,
                check=False,
            )
            stdout = (proc.stdout or "")[-2000:]
            stderr = (proc.stderr or "")[-2000:]
            return (
                f"exit={proc.returncode} level={level}\n"
                f"STDOUT:\n{stdout}\nSTDERR:\n{stderr}"
            )
        except Exception as exc:  # noqa: BLE001
            return _tool_error(exc)

    return {
        "list_workspace_files": list_workspace_files,
        "read_workspace_file": read_workspace_file,
        "write_workspace_file": write_workspace_file,
        "apply_workspace_patch": apply_workspace_patch,
        "run_public_check": run_public_check,
        "final_answer": FinalAnswerTool(),
    }


def register_repository_tools(registry: ToolRegistry) -> None:
    def _factory(tool_id: str):
        def build(context: ToolBuildContext) -> Any:
            # final_answer does not require workspace_ref.
            if tool_id == "final_answer":
                from smolagents import FinalAnswerTool

                return FinalAnswerTool()
            return _build_repository_tools(context)[tool_id]

        return build

    for tool_id in REPOSITORY_TOOL_IDS:
        if registry.has(tool_id):
            # final_answer may already be registered by BBEH tools.
            continue
        registry.register(tool_id, _factory(tool_id))
