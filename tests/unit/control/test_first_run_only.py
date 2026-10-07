"""First-run-only mode (joint experiment): a milestone ends at its first gate -- no probe, no default
repair, no row, no resample -- and is neither retried nor searched."""

from __future__ import annotations

import asyncio

import pytest

from orchestra.control.fast_loop import controller as C
from orchestra.control.fast_loop.controller import FastLoopController
from orchestra.control.fast_loop.quality_trigger import build_incumbent_record
from orchestra.control.task_state import SubtaskAttempt, SubtaskState, SubtaskStatus, TaskExecutionState
from orchestra.decomposition.schemas import BudgetSpec, SubtaskSpec


def _state(status: SubtaskStatus) -> TaskExecutionState:
    sub = SubtaskState(
        spec=SubtaskSpec(subtask_id="s1", title="t", objective="o", dependencies=[],
                         keystone_harness_id="repository_test_harness",
                         local_graph_template="configs/graphs/codex_single_implementer.yaml",
                         budget=BudgetSpec(max_llm_calls=1, max_steps=1, timeout_seconds=60)),
        status=status, attempts=[SubtaskAttempt(attempt_id=1, status=status, metadata={})],
    )
    return TaskExecutionState.model_construct(task_id="t", subtasks={"s1": sub}, fast_loop_states={})


def _controller(monkeypatch) -> FastLoopController:
    ctl = FastLoopController(runtime=None, artifact_store=None, task_checkpoint_store=None, persistence_search=True)

    async def no_search(**_kw):
        raise AssertionError("the search ran in first-run-only mode")

    monkeypatch.setattr(ctl, "_run_search", no_search)
    monkeypatch.setattr(ctl, "_write_ledger", lambda *a, **k: None)
    return ctl


def _run(ctl, state, incumbent=None):
    return asyncio.run(ctl.run(state=state, subtask_id="s1", context=None, initial_artifacts=None, incumbent=incumbent))


def test_failed_first_run_ends_failed_without_search_or_retry(monkeypatch) -> None:
    monkeypatch.setenv(C.FIRST_RUN_ONLY_ENV, "1")
    out = _run(_controller(monkeypatch), _state(SubtaskStatus.RETRY_PENDING))
    sub = out.subtasks["s1"]
    assert sub.status is SubtaskStatus.FAILED  # not RETRY_PENDING: the scheduler will not run it again
    assert "first-run only" in (sub.failure_message or "")
    assert out.fast_loop_states["s1"].candidates == []  # no probe, no R0, no row


def test_passing_first_run_is_kept_without_a_quality_search(monkeypatch) -> None:
    monkeypatch.setenv(C.FIRST_RUN_ONLY_ENV, "1")
    inc = build_incumbent_record(attempt_id=1, graph_hash="h", harness_score=0.9, behaviour_score=0.8,
                                 behaviour_failures=["t.py::test_x"], furthest_stage="spec_tests")
    out = _run(_controller(monkeypatch), _state(SubtaskStatus.RUNNING), incumbent=inc)
    fl = out.fast_loop_states["s1"]
    assert out.subtasks["s1"].status is SubtaskStatus.COMMITTED
    assert [c.candidate_id for c in fl.candidates] == [inc.candidate_id]
    assert fl.selected_candidate_id == inc.candidate_id


def test_mode_off_runs_the_search(monkeypatch) -> None:
    monkeypatch.delenv(C.FIRST_RUN_ONLY_ENV, raising=False)
    with pytest.raises(AssertionError, match="search ran"):
        _run(_controller(monkeypatch), _state(SubtaskStatus.RETRY_PENDING))
