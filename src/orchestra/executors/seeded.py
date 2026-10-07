"""Seeded first runs and pair-only searches (joint first-pass / playbook experiment, 2026-10-07).

A *repair pair* compares the default repair (R0) with a playbook row on the same incumbent, with
the same evidence. To pin the incumbent across runs, the milestone's first stage is *replayed*:
every agent node of the first attempt (it works in ``tasks/<task>/workspaces/<milestone>/repo``)
makes the workspace identical to a stored first-run workspace instead of calling a model, so the
gate then grades exactly that code and the search starts from it. Candidate workspaces
(``subtasks/<milestone>/candidates/...``) are never seeded.

``ADAMAS_PAIR_ONLY`` restricts the search that follows to R0 plus the forced row
(``ADAMAS_FORCE_ROW``): no probe (the incumbent's own gate failures are the evidence), no node
resample.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

SEED_DIR_ENV = "ADAMAS_SEED_FIRST_RUN"          # path to the stored first-run repository
SEED_MILESTONE_ENV = "ADAMAS_SEED_MILESTONE"     # the milestone it belongs to
PAIR_ONLY_ENV = "ADAMAS_PAIR_ONLY"
_SKIP = {".git", "spec_tests", "spec_tests_soft", "__pycache__", ".pytest_cache", ".mypy_cache", "repair_evidence"}


def _on(name: str) -> bool:
    return (os.environ.get(name) or "").strip() not in ("", "0", "false")


def pair_only() -> bool:
    return _on(PAIR_ONLY_ENV)


def seed_for(workspace_ref: str | None) -> Path | None:
    """The seed repository when ``workspace_ref`` is the seeded milestone's first-attempt workspace."""
    seed = (os.environ.get(SEED_DIR_ENV) or "").strip()
    mid = (os.environ.get(SEED_MILESTONE_ENV) or "").strip()
    if not seed or not mid or not workspace_ref:
        return None
    parts = Path(workspace_ref).parts
    if "workspaces" in parts and mid in parts and "candidates" not in parts:
        i = parts.index("workspaces")
        if i + 1 < len(parts) and parts[i + 1] == mid:
            return Path(seed)
    return None


def sync_tree(seed: Path, workspace: Path) -> list[str]:
    """Make ``workspace`` match ``seed`` (git metadata, frozen suites and caches left alone). Returns changed paths."""
    changed: list[str] = []

    def skip(rel: Path) -> bool:
        return any(p in _SKIP for p in rel.parts)

    for src in seed.rglob("*"):
        rel = src.relative_to(seed)
        if skip(rel) or src.is_dir():
            continue
        dst = workspace / rel
        if not dst.exists() or dst.read_bytes() != src.read_bytes():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            changed.append(rel.as_posix())
    for dst in sorted(workspace.rglob("*"), reverse=True):
        rel = dst.relative_to(workspace)
        if skip(rel):
            continue
        if not (seed / rel).exists():
            if dst.is_dir():
                if not any(dst.iterdir()):
                    dst.rmdir()
            else:
                dst.unlink()
                changed.append(rel.as_posix())
    return changed


__all__ = ["PAIR_ONLY_ENV", "SEED_DIR_ENV", "SEED_MILESTONE_ENV", "pair_only", "seed_for", "sync_tree"]
