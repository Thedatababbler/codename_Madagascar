"""Candidate workspaces must be isolated from each other and from base."""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestra.control.fast_loop.workspace import GitCandidateWorkspaceManager

FIXTURE = Path("tests/fixtures/codex_tiny_repo").resolve()


@pytest.mark.asyncio
async def test_parallel_candidates_do_not_see_each_other(tmp_path):
    mgr = GitCandidateWorkspaceManager()
    base = await mgr.prepare_base_snapshot(
        source_repo=str(FIXTURE),
        run_dir=str(tmp_path),
        task_id="t1",
        subtask_id="s1",
    )
    a = await mgr.fork_candidate_workspace(
        base=base,
        run_dir=str(tmp_path),
        task_id="t1",
        subtask_id="s1",
        candidate_id="cand_a",
    )
    b = await mgr.fork_candidate_workspace(
        base=base,
        run_dir=str(tmp_path),
        task_id="t1",
        subtask_id="s1",
        candidate_id="cand_b",
    )
    Path(a.path, "calculator.py").write_text(
        "def add(a, b):\n    return a + 100\n", encoding="utf-8"
    )
    Path(b.path, "calculator.py").write_text(
        "def add(a, b):\n    return a + 200\n", encoding="utf-8"
    )
    assert "100" in Path(a.path, "calculator.py").read_text(encoding="utf-8")
    assert "200" in Path(b.path, "calculator.py").read_text(encoding="utf-8")
    # Base unchanged.
    base_text = Path(base.path, "calculator.py").read_text(encoding="utf-8")
    assert "100" not in base_text
    assert "200" not in base_text
    assert a.path != b.path
    assert Path(a.path).resolve() != Path(base.path).resolve()
