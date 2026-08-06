"""Workspace-owned cross-milestone memory (scheme A: CHANGELOG on disk).

AdaMAS appends a structured changelog into the canonical workspace after each
successful commit. Later milestones/agents read the same file via the forked
workspace — no Codex thread resume required.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

CHANGELOG_NAME = "ADAMAS_CHANGELOG.md"
DECISIONS_NAME = "ADAMAS_DECISIONS.md"


def changelog_path(workspace: Path) -> Path:
    return Path(workspace) / CHANGELOG_NAME


def read_changelog(workspace: Path, *, max_chars: int = 12000) -> str:
    path = changelog_path(workspace)
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 20] + "\n\n…(changelog truncated)…\n"


def _run_git(workspace: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(workspace), *args],
        check=False,
        capture_output=True,
        text=True,
    )


def append_changelog_entry(
    workspace: Path,
    *,
    subtask_id: str,
    role: str | None,
    changed_files: list[str],
    revision: str | None,
    summary: str | None = None,
    extra: dict[str, Any] | None = None,
    git_commit: bool = True,
) -> Path:
    """Append one milestone memory entry and optionally commit it."""
    ws = Path(workspace)
    path = changelog_path(ws)
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    files = sorted({str(p).replace("\\", "/") for p in changed_files if p})
    lines = [
        "",
        f"## [{subtask_id}] {ts}",
        f"- role: `{role or 'unknown'}`",
        f"- revision: `{revision or ''}`",
        f"- changed_files ({len(files)}):",
    ]
    if files:
        lines.extend(f"  - `{f}`" for f in files[:80])
        if len(files) > 80:
            lines.append(f"  - … +{len(files) - 80} more")
    else:
        lines.append("  - (none recorded)")
    if summary and str(summary).strip():
        lines.append(f"- summary: {str(summary).strip()[:500]}")
    if extra:
        for key in sorted(extra):
            lines.append(f"- {key}: {extra[key]}")
    lines.append("")

    if not path.exists():
        header = (
            "# AdaMAS Workspace Changelog\n\n"
            "Runner-owned cross-milestone memory. Downstream agents must read "
            "this file before editing. Do not delete prior entries.\n"
        )
        path.write_text(header + "\n".join(lines), encoding="utf-8")
    else:
        with path.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(lines))

    if git_commit and (ws / ".git").exists():
        _run_git(ws, "add", "--", CHANGELOG_NAME)
        # Commit even if only changelog changed.
        msg = f"adamas changelog after {subtask_id}"
        _run_git(ws, "commit", "-m", msg, "--allow-empty")
    return path


def memory_brief_for_milestone(workspace: Path) -> str:
    """Markdown snippet injected into MILESTONE.md."""
    text = read_changelog(workspace)
    if not text.strip():
        return (
            "## Shared workspace memory\n"
            f"- No prior `{CHANGELOG_NAME}` yet (first milestone).\n"
            f"- After this milestone commits, AdaMAS will append to `{CHANGELOG_NAME}`.\n"
        )
    return (
        "## Shared workspace memory\n"
        f"Read `{CHANGELOG_NAME}` for prior milestone modification logs "
        "(shared across milestones/agents).\n\n"
        "<changelog_excerpt>\n"
        f"{text.strip()}\n"
        "</changelog_excerpt>\n"
    )
