"""Atomic winner commit: losers never pollute canonical base."""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestra.control.fast_loop.workspace import (
    CandidateWorkspaceError,
    GitCandidateWorkspaceManager,
)

FIXTURE = Path("tests/fixtures/codex_tiny_repo").resolve()


@pytest.mark.asyncio
async def test_winner_commit_and_idempotent_loser_isolation(tmp_path):
    mgr = GitCandidateWorkspaceManager()
    base = await mgr.prepare_base_snapshot(
        source_repo=str(FIXTURE),
        run_dir=str(tmp_path),
        task_id="t1",
        subtask_id="s1",
    )
    expected_rev = base.base_revision
    loser = await mgr.fork_candidate_workspace(
        base=base,
        run_dir=str(tmp_path),
        task_id="t1",
        subtask_id="s1",
        candidate_id="loser",
    )
    winner = await mgr.fork_candidate_workspace(
        base=base,
        run_dir=str(tmp_path),
        task_id="t1",
        subtask_id="s1",
        candidate_id="winner",
    )
    Path(loser.path, "calculator.py").write_text(
        "def add(a, b):\n    return a * b\n", encoding="utf-8"
    )
    Path(winner.path, "calculator.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8"
    )
    committed = await mgr.commit_winner(
        base=base,
        winner=winner,
        expected_base_revision=expected_rev,
    )
    text = Path(committed.path, "calculator.py").read_text(encoding="utf-8")
    assert "return a + b" in text
    assert "return a * b" not in text

    # Drift fail-closed.
    Path(base.path, "extra.txt").write_text("dirty", encoding="utf-8")
    with pytest.raises(CandidateWorkspaceError):
        await mgr.commit_winner(
            base=base,
            winner=winner,
            expected_base_revision=expected_rev,
        )


@pytest.mark.asyncio
async def test_commit_idempotent_when_already_applied(tmp_path):
    mgr = GitCandidateWorkspaceManager()
    base = await mgr.prepare_base_snapshot(
        source_repo=str(FIXTURE),
        run_dir=str(tmp_path),
        task_id="t1",
        subtask_id="s1",
    )
    winner = await mgr.fork_candidate_workspace(
        base=base,
        run_dir=str(tmp_path),
        task_id="t1",
        subtask_id="s1",
        candidate_id="winner",
    )
    # No changes → no-op commit succeeds.
    committed = await mgr.commit_winner(
        base=base,
        winner=winner,
        expected_base_revision=base.base_revision,
    )
    assert Path(committed.path).exists()
