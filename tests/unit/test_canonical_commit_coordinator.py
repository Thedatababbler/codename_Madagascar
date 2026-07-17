"""Canonical commits belong to the scheduler coordinator, not workers."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestra.control.canonical_workspace import CanonicalTaskWorkspaceManager
from orchestra.control.fast_loop.workspace import GitCandidateWorkspaceManager
from orchestra.control.ready_scheduler import (
    ReadySubtaskScheduler,
    SubtaskExecutionResult,
    SubtaskExecutionStatus,
)
from orchestra.control.task_state import (
    SubtaskStatus,
    TaskExecutionState,
    WorkspaceCommitStatus,
)
from orchestra.decomposition.fallback import build_single_subtask_plan

FIXTURE = Path("tests/fixtures/codex_tiny_repo").resolve()


@pytest.mark.asyncio
async def test_worker_does_not_mutate_canonical_workspace(tmp_path):
    mgr = GitCandidateWorkspaceManager()
    canonical_mgr = CanonicalTaskWorkspaceManager(mgr)
    can = await canonical_mgr.prepare(
        source_repo=str(FIXTURE), run_dir=str(tmp_path), task_id="t"
    )
    before = Path(can.path, "calculator.py").read_text(encoding="utf-8")
    fork = await canonical_mgr.fork_subtask_workspace(
        canonical=can,
        run_dir=str(tmp_path),
        task_id="t",
        subtask_id="s1",
    )
    Path(fork.path, "calculator.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8"
    )
    # Worker-only mutation: canonical unchanged.
    assert Path(can.path, "calculator.py").read_text(encoding="utf-8") == before


@pytest.mark.asyncio
async def test_canonical_commit_occurs_under_scheduler_lock(tmp_path):
    plan = build_single_subtask_plan(
        task_id="lock",
        objective="x",
        local_graph_template="configs/graphs/codex_single_implementer.yaml",
        keystone_harness_id="repository_test_harness",
    )
    state = TaskExecutionState.from_plan(plan)
    scheduler = object.__new__(ReadySubtaskScheduler)
    scheduler._state_lock = asyncio.Lock()
    scheduler.task_checkpoint_store = MagicMock()
    scheduler.task_checkpoint_store.save = AsyncMock()
    called = {"in_lock": False}

    async def fake_unlocked(**kwargs):  # noqa: ANN003
        called["in_lock"] = scheduler._state_lock.locked()

    scheduler._commit_subtask_result_unlocked = fake_unlocked  # type: ignore[method-assign]
    result = SubtaskExecutionResult(
        subtask_id="main",
        expected_state_version=0,
        local_subtask_state=state.subtasks["main"],
        execution_status=SubtaskExecutionStatus.FAILED,
    )
    await ReadySubtaskScheduler._commit_subtask_result(
        scheduler,
        task_plan=plan,
        state=state,
        result=result,
        context=MagicMock(run_dir=tmp_path),
    )
    assert called["in_lock"] is True


@pytest.mark.asyncio
async def test_commit_record_is_idempotent(tmp_path):
    mgr = GitCandidateWorkspaceManager()
    canonical_mgr = CanonicalTaskWorkspaceManager(mgr)
    can = await canonical_mgr.prepare(
        source_repo=str(FIXTURE), run_dir=str(tmp_path), task_id="idem"
    )
    winner = await canonical_mgr.fork_subtask_workspace(
        canonical=can,
        run_dir=str(tmp_path),
        task_id="idem",
        subtask_id="s1",
    )
    Path(winner.path, "calculator.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8"
    )
    Path(winner.path, "extra.txt").write_text("x\n", encoding="utf-8")
    cs = await mgr.collect_changeset(winner)
    promoted, rev1 = await canonical_mgr.transactional_commit(
        canonical=can,
        winner_workspace=winner,
        change_set=cs,
        run_dir=str(tmp_path),
        task_id="idem",
        subtask_id="s1",
        attempt_id=1,
        commit_message="c1",
        harness_command=["python", "-m", "pytest", "-q"],
        harness_timeout=30,
    )
    assert Path(promoted.path, "extra.txt").read_text(encoding="utf-8") == "x\n"
    assert rev1
    # Idempotent coordinator recovery: same change_set_hash already COMMITTED.
    plan = build_single_subtask_plan(
        task_id="idem",
        objective="x",
        local_graph_template="configs/graphs/codex_single_implementer.yaml",
        keystone_harness_id="repository_test_harness",
    )
    state = TaskExecutionState.from_plan(plan)
    state.canonical_workspace_ref = promoted.path
    state.canonical_revision = rev1
    from orchestra.control.task_state import WorkspaceCommitRecord

    state.workspace_commit_records.append(
        WorkspaceCommitRecord(
            record_id="r1",
            task_id="idem",
            subtask_id="main",
            attempt_id=1,
            change_set_hash=cs.file_manifest_hash,
            status=WorkspaceCommitStatus.COMMITTED,
            committed_revision=rev1,
        )
    )
    sub = state.subtasks["main"].model_copy(deep=True)
    sub.status = SubtaskStatus.AWAITING_CANONICAL_COMMIT
    sub.candidate_artifacts = []
    scheduler = object.__new__(ReadySubtaskScheduler)
    scheduler._state_lock = asyncio.Lock()
    scheduler.task_checkpoint_store = MagicMock()
    scheduler.task_checkpoint_store.save = AsyncMock()
    scheduler.canonical = canonical_mgr
    scheduler._candidate_ws = mgr
    result = SubtaskExecutionResult(
        subtask_id="main",
        expected_state_version=0,
        base_canonical_revision=can.base_revision,
        candidate_workspace_ref=winner.path,
        workspace_change_set=cs,
        local_subtask_state=sub,
        execution_status=SubtaskExecutionStatus.SUCCESS_PENDING_COMMIT,
        candidate_harness_passed=True,
        graph_template=sub.spec.local_graph_template,
    )
    await ReadySubtaskScheduler._commit_subtask_result_unlocked(
        scheduler,
        task_plan=plan,
        state=state,
        result=result,
        context=MagicMock(run_dir=tmp_path),
    )
    assert state.subtasks["main"].status is SubtaskStatus.COMMITTED


@pytest.mark.asyncio
async def test_revision_drift_triggers_controlled_reapply(tmp_path):
    mgr = GitCandidateWorkspaceManager()
    canonical_mgr = CanonicalTaskWorkspaceManager(mgr)
    can = await canonical_mgr.prepare(
        source_repo=str(FIXTURE), run_dir=str(tmp_path), task_id="drift"
    )
    r0 = can.base_revision
    w1 = await canonical_mgr.fork_subtask_workspace(
        canonical=can, run_dir=str(tmp_path), task_id="drift", subtask_id="s1"
    )
    w2 = await canonical_mgr.fork_subtask_workspace(
        canonical=can, run_dir=str(tmp_path), task_id="drift", subtask_id="s2"
    )
    Path(w1.path, "calculator.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8"
    )
    Path(w1.path, "file_a.py").write_text("a=1\n", encoding="utf-8")
    Path(w2.path, "calculator.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8"
    )
    Path(w2.path, "file_b.py").write_text("b=1\n", encoding="utf-8")
    cs1 = await mgr.collect_changeset(w1)
    cs2 = await mgr.collect_changeset(w2)
    can, _ = await canonical_mgr.transactional_commit(
        canonical=can,
        winner_workspace=w1,
        change_set=cs1,
        run_dir=str(tmp_path),
        task_id="drift",
        subtask_id="s1",
        attempt_id=1,
        commit_message="s1",
        harness_command=["python", "-m", "pytest", "-q"],
        harness_timeout=30,
    )
    assert can.base_revision != r0
    # S2 changeset from R0 applied onto R1 staging.
    can2, _ = await canonical_mgr.transactional_commit(
        canonical=can,
        winner_workspace=w2,
        change_set=cs2,
        run_dir=str(tmp_path),
        task_id="drift",
        subtask_id="s2",
        attempt_id=1,
        commit_message="s2",
        harness_command=["python", "-m", "pytest", "-q"],
        harness_timeout=30,
    )
    assert Path(can2.path, "file_a.py").is_file()
    assert Path(can2.path, "file_b.py").is_file()


@pytest.mark.asyncio
async def test_merge_conflict_never_marks_subtask_committed(tmp_path):
    mgr = GitCandidateWorkspaceManager()
    canonical_mgr = CanonicalTaskWorkspaceManager(mgr)
    can = await canonical_mgr.prepare(
        source_repo=str(FIXTURE), run_dir=str(tmp_path), task_id="conf"
    )
    w1 = await canonical_mgr.fork_subtask_workspace(
        canonical=can, run_dir=str(tmp_path), task_id="conf", subtask_id="s1"
    )
    w2 = await canonical_mgr.fork_subtask_workspace(
        canonical=can, run_dir=str(tmp_path), task_id="conf", subtask_id="s2"
    )
    Path(w1.path, "calculator.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8"
    )
    Path(w2.path, "calculator.py").write_text(
        "def add(a, b):\n    return a * b\n", encoding="utf-8"
    )
    cs1 = await mgr.collect_changeset(w1)
    cs2 = await mgr.collect_changeset(w2)
    can, _ = await canonical_mgr.transactional_commit(
        canonical=can,
        winner_workspace=w1,
        change_set=cs1,
        run_dir=str(tmp_path),
        task_id="conf",
        subtask_id="s1",
        attempt_id=1,
        commit_message="s1",
        harness_command=["python", "-m", "pytest", "-q"],
        harness_timeout=30,
    )
    from orchestra.control.canonical_workspace import CanonicalCommitError

    with pytest.raises(CanonicalCommitError) as exc:
        await canonical_mgr.transactional_commit(
            canonical=can,
            winner_workspace=w2,
            change_set=cs2,
            run_dir=str(tmp_path),
            task_id="conf",
            subtask_id="s2",
            attempt_id=1,
            commit_message="s2",
            harness_command=["python", "-m", "pytest", "-q"],
            harness_timeout=30,
        )
    assert exc.value.conflict is True
    # Canonical still has S1 content.
    assert "return a + b" in Path(can.path, "calculator.py").read_text(encoding="utf-8")
