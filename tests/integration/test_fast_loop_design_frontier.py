"""A milestone's design search, followed all the way through a scheduler run.

The unit tests establish that the frontier is built correctly from candidate
records. What they cannot establish is that a real run produces candidates whose
axes differ at all -- and that is the property the whole experiment rests on. A
search whose candidates always land on the same point is a ranking with extra
steps, and it would look identical in every unit test.

So this drives the actual fast loop with a backend and a harness that give each
candidate a different score and a different cost, and asserts that two mutually
non-dominating designs survive to be recorded.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fast_loop_scaffold import run_scheduler

from orchestra.control.task_state import TaskExecutionState

#: Chosen so exactly one pair trades quality against cost: candidate 1 scores 0.5
#: cheaply, candidate 2 scores 0.9 dearly, and candidate 3 scores 0.5 dearly and is
#: therefore dominated by candidate 1. The first attempt fails, which is what makes
#: this the repair path rather than a quality search.
#:
#: Candidate 3 shares candidate 1's verdict deliberately. Domination only applies
#: within a verdict — a candidate that failed the gate may not push a passing one
#: off the frontier — so a dominated point has to be on the same side of the gate
#: as the point that dominates it. The trailing entry is the commit's
#: re-verification on the canonical workspace, which must pass or the winner is
#: rolled back and there is nothing left to assert.
SCORES = [0.3, 0.5, 0.9, 0.5, 0.9]
PASSES = [False, False, True, False, True]


async def _run(tmp_path: Path, *, design_search: bool) -> TaskExecutionState:
    return await run_scheduler(
        tmp_path, design_search=design_search, scores=SCORES, passes=PASSES
    )


def _fast_loop(state: TaskExecutionState):  # noqa: ANN202 - FastLoopState
    assert state.fast_loop_states, "the gate had to fail for a search to happen at all"
    return next(iter(state.fast_loop_states.values()))


@pytest.mark.asyncio
async def test_the_search_tries_more_than_one_design(tmp_path: Path) -> None:
    fast_loop = _fast_loop(await _run(tmp_path, design_search=True))
    executed = [c for c in fast_loop.candidates if c.cost.backend_calls]
    assert len(executed) >= 2
    # Distinct graphs, not distinct labels: candidates that compile to the same
    # graph would be duplicate points on the frontier.
    assert len({c.graph_hash for c in executed}) == len(executed)


@pytest.mark.asyncio
async def test_two_designs_that_trade_off_both_survive(tmp_path: Path) -> None:
    fast_loop = _fast_loop(await _run(tmp_path, design_search=True))
    scored = [c for c in fast_loop.candidates if c.harness_score is not None]
    assert len(scored) >= 2
    # The property the experiment rests on: at least one pair where neither is
    # better on both axes, so the frontier has something to report.
    assert len(fast_loop.pareto_frontier) >= 2
    assert len(fast_loop.pareto_frontier) < len(scored) or len(scored) == 2
    assert fast_loop.selection_rule == "quality_first"


@pytest.mark.asyncio
async def test_the_committed_candidate_is_on_the_frontier(tmp_path: Path) -> None:
    fast_loop = _fast_loop(await _run(tmp_path, design_search=True))
    assert fast_loop.selected_candidate_id is not None
    assert fast_loop.selected_candidate_id in fast_loop.pareto_frontier


@pytest.mark.asyncio
async def test_candidates_carry_a_measured_cost(tmp_path: Path) -> None:
    """Without this the cost axis is unavailable and the frontier is quality-only."""
    fast_loop = _fast_loop(await _run(tmp_path, design_search=True))
    executed = [c for c in fast_loop.candidates if c.cost.backend_calls]
    assert all(c.cost.estimated_cost_usd > 0.0 for c in executed)
    assert len({round(c.cost.estimated_cost_usd, 6) for c in executed}) > 1


@pytest.mark.asyncio
async def test_a_repair_search_is_not_labelled_a_quality_search(tmp_path: Path) -> None:
    """The two have different success conditions and must stay distinguishable."""
    fast_loop = _fast_loop(await _run(tmp_path, design_search=True))
    assert fast_loop.search_reason != "quality"


@pytest.mark.asyncio
async def test_the_scalar_path_records_no_frontier(tmp_path: Path) -> None:
    """The control condition: same run, old selector, nothing to compare."""
    fast_loop = _fast_loop(await _run(tmp_path, design_search=False))
    assert fast_loop.pareto_frontier == []
    assert fast_loop.selection_rule == "scalar"
