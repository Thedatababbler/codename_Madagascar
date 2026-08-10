"""Searching a milestone that passed its gate, driven through a real scheduler run.

The unit tests pin the trigger's arithmetic and the selector's invariants. What
only a real run can show is that the path exists at all: the fast loop lives in the
scheduler's failure branch, and a milestone that commits used to return before the
loop was reached. Every assertion here is about a run whose gate *passed*.

The risk being guarded is specific. A search on already-good work can only be
justified if it cannot make things worse, so the cases below are the ways it could:
declining without breaking the milestone, refusing a higher-scoring candidate that
failed the gate, and staying switched off unless asked.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fast_loop_scaffold import run_scheduler

from orchestra.control.fast_loop.quality_trigger import (
    INCUMBENT_CANDIDATE_ID,
    QualityTrigger,
)
from orchestra.control.task_state import SubtaskStatus, TaskExecutionState

#: The first attempt passes with a poor score, which is the whole premise: the gate
#: says "safe to build on", the score says "not much of it works".
PASSING_BUT_POOR = 0.30

#: No candidate beats the incumbent's 0.30 by more than the quality tolerance, so
#: the search should decline. Every entry passes, so nothing here fails for the
#: wrong reason.
NO_IMPROVEMENT = {
    "scores": [PASSING_BUT_POOR, 0.30, 0.29, 0.30, 0.30],
    "passes": [True, True, True, True, True],
}

#: Candidate 2 is a real improvement, so the search should adopt it.
IMPROVEMENT = {
    "scores": [PASSING_BUT_POOR, 0.30, 0.95, 0.30, 0.95],
    "passes": [True, True, True, True, True],
}

#: Candidate 2 scores far higher but abandons the gate. Adopting it would trade a
#: milestone that works for one that does not.
IMPROVEMENT_THAT_FAILS_THE_GATE = {
    "scores": [PASSING_BUT_POOR, 0.30, 0.99, 0.30, 0.30],
    "passes": [True, True, False, True, True],
}

TRIGGER = QualityTrigger(enabled=True, min_score=0.8)


def _fast_loop(state: TaskExecutionState):  # noqa: ANN202 - FastLoopState
    assert state.fast_loop_states, (
        "no search ran: a passing gate still returns before the fast loop unless the "
        "quality trigger routes it there"
    )
    return next(iter(state.fast_loop_states.values()))


def _subtask(state: TaskExecutionState):  # noqa: ANN202 - SubtaskState
    return next(iter(state.subtasks.values()))


@pytest.mark.asyncio
async def test_a_passing_milestone_is_searched_when_it_scored_poorly(
    tmp_path: Path,
) -> None:
    state = await run_scheduler(
        tmp_path, design_search=True, quality_trigger=TRIGGER, **NO_IMPROVEMENT
    )

    fast_loop = _fast_loop(state)
    assert fast_loop.search_reason == "quality"
    # Candidates other than the incumbent actually ran, or nothing was searched.
    ran = [
        c
        for c in fast_loop.candidates
        if c.cost.backend_calls and not c.metadata.get("incumbent")
    ]
    assert ran, "the trigger fired but no candidate design was executed"


@pytest.mark.asyncio
async def test_the_search_is_off_unless_asked(tmp_path: Path) -> None:
    """The same poor-but-passing run, with no trigger configured."""
    state = await run_scheduler(tmp_path, design_search=True, **NO_IMPROVEMENT)

    assert state.fast_loop_states == {}
    assert _subtask(state).status in {
        SubtaskStatus.COMMITTED,
        SubtaskStatus.AWAITING_CANONICAL_COMMIT,
    }


@pytest.mark.asyncio
async def test_a_high_scoring_pass_is_not_searched(tmp_path: Path) -> None:
    """Above the threshold there is nothing to buy."""
    state = await run_scheduler(
        tmp_path,
        design_search=True,
        quality_trigger=TRIGGER,
        scores=[0.95, 0.95, 0.95, 0.95, 0.95],
        passes=[True, True, True, True, True],
    )

    assert state.fast_loop_states == {}


@pytest.mark.asyncio
async def test_declining_leaves_the_milestone_committed(tmp_path: Path) -> None:
    """The outcome that must not fail the run.

    A quality search runs on work that already passed, so "found nothing better" is
    a no-op. If it were recorded as a failure the milestone would be broken by the
    search it was subjected to, which is worse than never searching.
    """
    state = await run_scheduler(
        tmp_path, design_search=True, quality_trigger=TRIGGER, **NO_IMPROVEMENT
    )

    fast_loop = _fast_loop(state)
    sub = _subtask(state)
    assert fast_loop.selected_candidate_id == INCUMBENT_CANDIDATE_ID
    assert sub.status in {
        SubtaskStatus.COMMITTED,
        SubtaskStatus.AWAITING_CANONICAL_COMMIT,
    }
    assert sub.failure_reason is None
    assert sub.failure_message is None
    assert any("declined" in note for note in fast_loop.notes)


@pytest.mark.asyncio
async def test_the_incumbent_is_on_the_frontier_it_was_judged_against(
    tmp_path: Path,
) -> None:
    """Otherwise the search would pick the best alternative and commit it blind."""
    state = await run_scheduler(
        tmp_path, design_search=True, quality_trigger=TRIGGER, **NO_IMPROVEMENT
    )

    fast_loop = _fast_loop(state)
    assert INCUMBENT_CANDIDATE_ID in fast_loop.pareto_frontier


@pytest.mark.asyncio
async def test_a_genuinely_better_design_is_adopted(tmp_path: Path) -> None:
    """The search has to be able to succeed, not only to decline."""
    state = await run_scheduler(
        tmp_path, design_search=True, quality_trigger=TRIGGER, **IMPROVEMENT
    )

    fast_loop = _fast_loop(state)
    assert fast_loop.selected_candidate_id is not None
    assert fast_loop.selected_candidate_id != INCUMBENT_CANDIDATE_ID
    winner = next(
        c
        for c in fast_loop.candidates
        if c.candidate_id == fast_loop.selected_candidate_id
    )
    assert (winner.harness_score or 0.0) > PASSING_BUT_POOR
    assert _subtask(state).status in {
        SubtaskStatus.COMMITTED,
        SubtaskStatus.AWAITING_CANONICAL_COMMIT,
    }


@pytest.mark.asyncio
async def test_a_higher_score_that_fails_the_gate_is_refused(tmp_path: Path) -> None:
    """Quality never buys its way past safety."""
    state = await run_scheduler(
        tmp_path,
        design_search=True,
        quality_trigger=TRIGGER,
        **IMPROVEMENT_THAT_FAILS_THE_GATE,
    )

    fast_loop = _fast_loop(state)
    winner_id = fast_loop.selected_candidate_id
    if winner_id != INCUMBENT_CANDIDATE_ID:
        winner = next(
            c for c in fast_loop.candidates if c.candidate_id == winner_id
        )
        # A candidate may only displace the incumbent by passing its own gate.
        assert winner.status.value in {"valid", "committed"}
    sub = _subtask(state)
    assert sub.status in {
        SubtaskStatus.COMMITTED,
        SubtaskStatus.AWAITING_CANONICAL_COMMIT,
    }
    assert sub.failure_reason is None
