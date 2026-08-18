"""A real search where the difference between designs is one test in fifty-eight.

This is the shape of the recorded imapclient search (EXP-20260810-05): candidates
graded against 58 authored tests, 43 of which pass for all of them and 7 of which
fail for all of them. Diluted across the whole suite the candidates differ by 0.017,
under the 0.02 quality epsilon, so the frontier collapsed and selection fell through
to price. Over the tests that moved, the same candidates differ by half the axis.

The assertions are about a scheduler run rather than the selector alone, because the
per-test identities have to survive the whole path — harness stdout, the harness
artifact, the candidate record, the selector — and every one of those hops previously
carried only counts.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fast_loop_scaffold import run_scheduler

from orchestra.control.fast_loop.quality_trigger import (
    INCUMBENT_CANDIDATE_ID,
    QualityTrigger,
)
from orchestra.control.task_state import TaskExecutionState

#: 58 authored tests, as recorded.
UNITS = 58

#: Seven no candidate passes, and forty-three every candidate passes. Only the two
#: named `moved_*` differ, which is what selection has to be able to see.
CONSTANT = [f"spec_tests/test_c.py::test_always_fails_{i}" for i in range(7)]
MOVED_A = "spec_tests/test_a.py::test_moved_a"
MOVED_B = "spec_tests/test_b.py::test_moved_b"

#: Per harness invocation: first attempt, then one per candidate, then the commit's
#: re-verification. The incumbent fails both moving tests; candidate 2 fixes one.
FAILURES = [
    CONSTANT + [MOVED_A, MOVED_B],
    CONSTANT + [MOVED_A, MOVED_B],
    CONSTANT + [MOVED_A],
    CONSTANT + [MOVED_A, MOVED_B],
    CONSTANT + [MOVED_A],
]

TRIGGER = QualityTrigger(enabled=True, min_score=0.95)


def _fast_loop(state: TaskExecutionState):  # noqa: ANN202 - FastLoopState
    assert state.fast_loop_states, "no search ran"
    return next(iter(state.fast_loop_states.values()))


@pytest.mark.asyncio
async def test_per_test_identities_survive_the_whole_path(tmp_path: Path) -> None:
    state = await run_scheduler(
        tmp_path,
        design_search=True,
        quality_trigger=TRIGGER,
        scores=[0.95] * 5,
        passes=[True] * 5,
        failed_ids=FAILURES,
        units=UNITS,
    )

    fast_loop = _fast_loop(state)
    executed = [c for c in fast_loop.candidates if c.behaviour_total is not None]
    assert executed, "no candidate carried a behavioural stage"
    assert all(c.behaviour_total == UNITS for c in executed)
    assert any(c.behaviour_failures for c in executed)


@pytest.mark.asyncio
async def test_the_constant_tests_do_not_dilute_the_comparison(tmp_path: Path) -> None:
    """Raw behaviour separates these candidates by 1/58; the refinement by 1/2."""
    state = await run_scheduler(
        tmp_path,
        design_search=True,
        quality_trigger=TRIGGER,
        scores=[0.95] * 5,
        passes=[True] * 5,
        failed_ids=FAILURES,
        units=UNITS,
    )

    fast_loop = _fast_loop(state)
    refined = {
        c.candidate_id: c.comparable_quality
        for c in fast_loop.candidates
        if c.comparable_quality is not None
    }
    assert len(refined) >= 2, "the selector did not compare over the moving tests"
    raw = [
        c.behaviour_score
        for c in fast_loop.candidates
        if c.behaviour_score is not None
    ]
    assert max(raw) - min(raw) < 0.02, "premise: raw scores are inside epsilon"
    assert max(refined.values()) - min(refined.values()) >= 0.5


@pytest.mark.asyncio
async def test_the_better_design_wins_where_raw_scores_tied(tmp_path: Path) -> None:
    """The consequence: a real improvement is adopted instead of read as a tie.

    On the raw axis the incumbent and the improved candidate differ by less than
    epsilon, so nothing dominates anything and `quality_first` breaks the tie on
    cost — which the incumbent wins, having already been paid for.
    """
    state = await run_scheduler(
        tmp_path,
        design_search=True,
        quality_trigger=TRIGGER,
        scores=[0.95] * 5,
        passes=[True] * 5,
        failed_ids=FAILURES,
        units=UNITS,
    )

    fast_loop = _fast_loop(state)
    assert fast_loop.selected_candidate_id is not None
    assert fast_loop.selected_candidate_id != INCUMBENT_CANDIDATE_ID
    winner = next(
        c for c in fast_loop.candidates if c.candidate_id == fast_loop.selected_candidate_id
    )
    assert MOVED_B not in winner.behaviour_failures
