"""Cross-milestone memory kept in the run directory (prompt-only delivery).

AdaMAS appends a structured changelog under the run directory after each
successful commit. Later milestones receive it as a **prompt prelude**; the
agent workspace stays free of AdaMAS bookkeeping files.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

CHANGELOG_NAME = "ADAMAS_CHANGELOG.md"
MEMORY_DIRNAME = "adamas_memory"
SUMMARY_MAX_CHARS = 1200


def memory_dir_for_run(run_dir: Path) -> Path:
    return Path(run_dir) / MEMORY_DIRNAME


def changelog_path(memory_dir: Path) -> Path:
    return Path(memory_dir) / CHANGELOG_NAME


def read_changelog(memory_dir: Path, *, max_chars: int = 12000) -> str:
    path = changelog_path(memory_dir)
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 20] + "\n\n…(changelog truncated)…\n"


def append_changelog_entry(
    memory_dir: Path,
    *,
    subtask_id: str,
    role: str | None,
    changed_files: list[str],
    revision: str | None,
    summary: str | None = None,
    extra: dict[str, Any] | None = None,
) -> Path:
    """Append one milestone memory entry to the runner-side changelog."""
    target = Path(memory_dir)
    target.mkdir(parents=True, exist_ok=True)
    path = changelog_path(target)
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
        # Roomier than a one-line label because this now carries the committing
        # agent's own account of what it decided, not a fixed caption: the
        # interface shapes and formats a later milestone must extend do not fit
        # in a clause. Four milestones at this cap stay well inside the
        # changelog's own read limit.
        text = str(summary).strip()[:SUMMARY_MAX_CHARS]
        if "\n" in text or len(text) > 120:
            lines.append("- summary:")
            lines.extend(f"  > {line}" for line in text.splitlines())
        else:
            lines.append(f"- summary: {text}")
    if extra:
        for key in sorted(extra):
            lines.append(f"- {key}: {extra[key]}")
    lines.append("")

    if not path.exists():
        header = (
            "# AdaMAS Milestone Memory\n\n"
            "Runner-owned cross-milestone log injected into later agent prompts.\n"
        )
        path.write_text(header + "\n".join(lines), encoding="utf-8")
    else:
        with path.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(lines))
    return path


def memory_brief_for_milestone(memory_dir: Path) -> str:
    """Prompt section describing what earlier milestones already committed."""
    text = read_changelog(memory_dir)
    if not text.strip():
        return (
            "## Shared milestone memory\n"
            "- No earlier milestone has committed yet; you are first.\n"
        )
    return (
        "## Shared milestone memory\n"
        "Earlier milestones already committed the changes below into the "
        "repository you now see. Extend them; do not redo or rename them.\n"
        "Each `summary` is the committing agent's own account of what it decided. "
        "Treat it as the fastest way to find what a module already settled, and "
        "the code as the authority when the two disagree.\n\n"
        "<changelog_excerpt>\n"
        f"{text.strip()}\n"
        "</changelog_excerpt>\n"
    )
