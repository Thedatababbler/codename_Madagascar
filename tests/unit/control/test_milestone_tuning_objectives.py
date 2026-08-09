"""What the fast loop optimises once a milestone's gate has run."""

from __future__ import annotations

import json

import pytest

from orchestra.control.fast_loop.objectives import (
    MilestoneObjective,
    TuningWeights,
    rank,
)
from orchestra.control.fast_loop.schemas import (
    CandidateRecord,
    CandidateStatus,
    CostRecord,
    FastLoopBudget,
)
from orchestra.control.fast_loop.selector import DeterministicCandidateSelector
from orchestra.harness.progress import parse_progress


def _objective(cid: str, **kwargs: object) -> MilestoneObjective:
    base = {
        "milestone_id": "m1",
        "candidate_id": cid,
        "gate_passed": False,
        "harness_score": None,
    }
    base.update(kwargs)
    return MilestoneObjective(**base)  # type: ignore[arg-type]


def test_a_passing_candidate_beats_a_cheaper_failing_one() -> None:
    """The gate is a hard partition, not a term in a weighted sum."""
    ordered = rank(
        [
            _objective("cheap_fail", gate_passed=False, harness_score=0.9, prompt_tokens=10),
            _objective("costly_pass", gate_passed=True, harness_score=1.0,
                       prompt_tokens=5_000_000),
        ]
    )

    assert ordered[0].candidate_id == "costly_pass"


def test_two_failures_are_separated_by_how_far_they_got() -> None:
    """This is the whole reason for grading the harness.

    With a bare pass/fail both of these score zero, the selector falls through
    to price, and the fast loop learns to fail cheaply.
    """
    ordered = rank(
        [
            _objective("barely_started", harness_score=0.15, prompt_tokens=100),
            _objective("nearly_there", harness_score=0.85, prompt_tokens=100),
        ]
    )

    assert [o.candidate_id for o in ordered] == ["nearly_there", "barely_started"]


def test_tokens_only_break_a_tie() -> None:
    """A cheaper candidate must not win by enough tokens to lose real progress."""
    ordered = rank(
        [
            _objective("cheap_worse", harness_score=0.5, prompt_tokens=1_000),
            _objective("dear_better", harness_score=0.7, prompt_tokens=900_000),
        ]
    )
    assert ordered[0].candidate_id == "dear_better"

    tied = rank(
        [
            _objective("dear", harness_score=0.7, prompt_tokens=900_000),
            _objective("cheap", harness_score=0.7, prompt_tokens=1_000),
        ]
    )
    assert tied[0].candidate_id == "cheap"


def test_cost_can_be_allowed_to_outrank_the_gate_but_never_by_default() -> None:
    candidates = [
        _objective("expensive_pass", gate_passed=True, harness_score=1.0,
                   prompt_tokens=100_000_000),
        _objective("cheap_near_miss", harness_score=0.95, prompt_tokens=1_000),
    ]

    assert rank(candidates)[0].candidate_id == "expensive_pass"
    aggressive = TuningWeights(allow_cost_to_outrank_gate=True, token_weight=0.05)
    assert rank(candidates, aggressive)[0].candidate_id == "cheap_near_miss"


def test_a_harness_that_reports_no_score_is_not_treated_as_zero() -> None:
    """Absence of a grade is not a grade of zero.

    Only some harnesses report progress; a passing candidate under a plain
    pytest command must not rank below a partially-failing one that happened to
    run under a harness that does.
    """
    passing = _objective("plain_pass", gate_passed=True, harness_score=None)
    partial = _objective("graded_partial", gate_passed=False, harness_score=0.6)

    assert passing.effective_score == 1.0
    assert rank([partial, passing])[0].candidate_id == "plain_pass"


def _candidate(cid: str, **kwargs: object) -> CandidateRecord:
    base = {
        "candidate_id": cid,
        "attempt_id": 1,
        "graph_hash": f"h-{cid}",
        "parent_graph_hash": "parent",
        "edits": [],
        "status": CandidateStatus.VALID,
        "quality_score": 1.0,
        "cost": CostRecord(backend_calls=1),
    }
    base.update(kwargs)
    return CandidateRecord(**base)  # type: ignore[arg-type]


def test_the_selector_reads_the_graded_score(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both candidates passed their gate; the further one should win."""
    winner = DeterministicCandidateSelector().select(
        [
            _candidate("partial", harness_score=0.4),
            _candidate("complete", harness_score=1.0),
        ],
        FastLoopBudget(),
    )

    assert winner is not None
    assert winner.candidate_id == "complete"


def test_the_selector_still_breaks_exact_ties_deterministically() -> None:
    winner = DeterministicCandidateSelector().select(
        [_candidate("b", harness_score=0.5), _candidate("a", harness_score=0.5)],
        FastLoopBudget(),
    )

    assert winner is not None
    assert winner.candidate_id == "a"


def test_progress_survives_the_trip_through_harness_stdout() -> None:
    """The score is produced in a subprocess and read back out of its output."""
    payload = {
        "score": 0.55,
        "level": "integration",
        "stages": [
            {"stage": "compile", "passed_units": 1, "total_units": 1, "weight": 0.15},
            {"stage": "imports", "passed_units": 8, "total_units": 10, "weight": 0.25},
        ],
        "furthest_stage": "imports",
    }
    stdout = "OK compileall\nADAMAS_HARNESS_SCORE " + json.dumps(payload) + "\n"

    score, stages, furthest = parse_progress(stdout)

    assert score == 0.55
    assert furthest == "imports"
    assert [(s.stage, s.passed_units, s.total_units) for s in stages] == [
        ("compile", 1, 1), ("imports", 8, 10)
    ]


def test_output_without_the_marker_reports_no_score() -> None:
    assert parse_progress("3 passed in 0.1s") == (None, [], "")
    assert parse_progress("ADAMAS_HARNESS_SCORE {not json") == (None, [], "")
