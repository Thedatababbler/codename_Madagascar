"""A search on a milestone that already passed must never make things worse.

The fast loop used to be reachable only through failure, which capped the tuning
population at the one repository whose gate fails (EXP-20260810-04). Searching a
milestone that passed opens the other three, but it also introduces a way to lose:
the milestone is already good enough to build on, so any outcome that replaces it
with something worse is a regression the old design could not produce.

These tests pin the three properties that make it safe: the search fires only on
measured evidence, the first pass competes, and safety outranks score.
"""

from __future__ import annotations

import pytest

from orchestra.control.fast_loop.pareto import (
    ParetoSelectionConfig,
    frontier,
    select_from_frontier,
)
from orchestra.control.fast_loop.quality_trigger import (
    INCUMBENT_CANDIDATE_ID,
    QualityTrigger,
    build_incumbent_record,
    quality_search_diagnosis,
)
from orchestra.control.fast_loop.schemas import (
    CandidateRecord,
    CandidateStatus,
    CostRecord,
)


def _candidate(
    cid: str,
    *,
    score: float | None,
    cost: float,
    status: CandidateStatus = CandidateStatus.VALID,
) -> CandidateRecord:
    return CandidateRecord(
        candidate_id=cid,
        attempt_id=2,
        graph_hash="h",
        parent_graph_hash="h",
        edits=[],
        status=status,
        harness_score=score,
        cost=CostRecord(prompt_tokens=100, completion_tokens=10, estimated_cost_usd=cost),
    )


class TestWhenItFires:
    def test_off_by_default(self):
        assert QualityTrigger().enabled is False
        assert (
            QualityTrigger().fires(gate_passed=True, harness_score=0.0) is False
        )

    def test_fires_only_below_the_threshold(self):
        trigger = QualityTrigger(enabled=True, min_score=0.8)

        assert trigger.fires(gate_passed=True, harness_score=0.79) is True
        assert trigger.fires(gate_passed=True, harness_score=0.8) is False
        assert trigger.fires(gate_passed=True, harness_score=1.0) is False

    def test_never_fires_on_a_failed_gate(self):
        """That path is the existing repair loop; firing here would double-search."""
        trigger = QualityTrigger(enabled=True, min_score=0.8)

        assert trigger.fires(gate_passed=False, harness_score=0.1) is False

    def test_an_unmeasured_score_does_not_fire(self):
        """Otherwise it would search every run and nothing could ever satisfy it."""
        trigger = QualityTrigger(enabled=True, min_score=0.8)

        assert trigger.fires(gate_passed=True, harness_score=None) is False

    def test_a_threshold_outside_zero_to_one_is_refused(self):
        with pytest.raises(ValueError, match="min_score"):
            QualityTrigger.from_mapping({"enabled": True, "min_score": 1.5})

    def test_config_round_trip(self):
        trigger = QualityTrigger.from_mapping({"enabled": True, "min_score": 0.75})

        assert trigger == QualityTrigger(enabled=True, min_score=0.75)
        assert QualityTrigger.from_mapping(None) == QualityTrigger()


class TestTheIncumbentCompetes:
    def test_the_incumbent_is_on_the_frontier(self):
        """It must be comparable, or the search cannot conclude 'nothing better'."""
        incumbent = build_incumbent_record(
            attempt_id=1,
            graph_hash="h",
            harness_score=0.55,
            cost=CostRecord(prompt_tokens=90, completion_tokens=9, estimated_cost_usd=1.5),
        )

        points = frontier([incumbent, _candidate("c1", score=0.40, cost=2.0)])

        assert INCUMBENT_CANDIDATE_ID in {c.candidate_id for c in points}

    def test_a_worse_and_dearer_candidate_is_dominated_by_the_incumbent(self):
        incumbent = build_incumbent_record(
            attempt_id=1,
            graph_hash="h",
            harness_score=0.55,
            cost=CostRecord(prompt_tokens=90, completion_tokens=9, estimated_cost_usd=1.0),
        )
        worse = _candidate("c1", score=0.30, cost=2.0)

        points = frontier([incumbent, worse])

        assert [c.candidate_id for c in points] == [INCUMBENT_CANDIDATE_ID]

    def test_the_incumbent_wins_when_nothing_improves_on_it(self):
        incumbent = build_incumbent_record(
            attempt_id=1,
            graph_hash="h",
            harness_score=0.55,
            cost=CostRecord(prompt_tokens=90, completion_tokens=9, estimated_cost_usd=1.0),
        )
        candidates = [incumbent, _candidate("c1", score=0.30, cost=2.0)]

        winner = select_from_frontier(frontier(candidates))

        assert winner is not None
        assert winner.candidate_id == INCUMBENT_CANDIDATE_ID

    def test_a_better_candidate_beats_the_incumbent(self):
        """The search has to be able to succeed, not only to decline."""
        incumbent = build_incumbent_record(
            attempt_id=1,
            graph_hash="h",
            harness_score=0.55,
            cost=CostRecord(prompt_tokens=90, completion_tokens=9, estimated_cost_usd=1.0),
        )
        better = _candidate("c1", score=0.90, cost=1.2)

        winner = select_from_frontier(frontier([incumbent, better]))

        assert winner is not None
        assert winner.candidate_id == "c1"

    def test_the_incumbent_carries_its_real_cost(self):
        """A free incumbent would dominate every candidate on cost alone."""
        incumbent = build_incumbent_record(
            attempt_id=1,
            graph_hash="h",
            harness_score=0.55,
            cost=CostRecord(prompt_tokens=90, completion_tokens=9, estimated_cost_usd=1.9),
        )

        assert incumbent.cost.estimated_cost_usd == pytest.approx(1.9)

    def test_the_incumbents_quality_is_its_graded_score_not_its_gate(self):
        """Its gate passed, so a gate-derived quality would read 1.0 and never lose."""
        incumbent = build_incumbent_record(
            attempt_id=1, graph_hash="h", harness_score=0.55
        )
        better = _candidate("c1", score=0.90, cost=0.1)

        winner = select_from_frontier(frontier([incumbent, better]))

        assert winner is not None
        assert winner.candidate_id == "c1"


class TestSafetyOutranksScore:
    def test_a_failing_candidate_does_not_win_against_a_passing_incumbent(self):
        """A higher score on work that fails the gate is not an improvement."""
        incumbent = build_incumbent_record(
            attempt_id=1,
            graph_hash="h",
            harness_score=0.55,
            cost=CostRecord(prompt_tokens=90, completion_tokens=9, estimated_cost_usd=1.0),
        )
        # Cheaper and higher-scoring, but its gate failed.
        rogue = _candidate(
            "c1", score=0.95, cost=0.2, status=CandidateStatus.HARNESS_FAILED
        )

        points = frontier([incumbent, rogue])
        winner = select_from_frontier(points)

        assert winner is not None
        assert winner.candidate_id == INCUMBENT_CANDIDATE_ID

    def test_the_frontier_itself_stays_honest_about_the_failing_point(self):
        """Selection prefers passing candidates; the frontier still records both."""
        incumbent = build_incumbent_record(
            attempt_id=1,
            graph_hash="h",
            harness_score=0.55,
            cost=CostRecord(prompt_tokens=90, completion_tokens=9, estimated_cost_usd=1.0),
        )
        rogue = _candidate(
            "c1", score=0.95, cost=0.2, status=CandidateStatus.HARNESS_FAILED
        )

        ids = {c.candidate_id for c in frontier([incumbent, rogue])}

        assert ids == {INCUMBENT_CANDIDATE_ID, "c1"}

    def test_disabling_gate_preference_does_not_reach_the_controller_invariant(self):
        """`require_gate_pass: false` can pick a failing winner — hence the guard.

        The controller refuses such a winner outright when an incumbent exists, and
        this documents why that guard is not redundant with the config.
        """
        incumbent = build_incumbent_record(
            attempt_id=1,
            graph_hash="h",
            harness_score=0.55,
            cost=CostRecord(prompt_tokens=90, completion_tokens=9, estimated_cost_usd=1.0),
        )
        rogue = _candidate(
            "c1", score=0.95, cost=0.2, status=CandidateStatus.HARNESS_FAILED
        )
        config = ParetoSelectionConfig(require_gate_pass=False)

        winner = select_from_frontier(frontier([incumbent, rogue], config), config)

        assert winner is not None
        assert winner.candidate_id == "c1"


class TestTheDiagnosis:
    def test_it_points_the_generator_at_the_weakest_stage(self):
        incumbent = build_incumbent_record(
            attempt_id=1, graph_hash="h", harness_score=0.55, furthest_stage="spec_tests"
        )

        diagnosis = quality_search_diagnosis(incumbent)

        assert diagnosis.furthest_stage == "spec_tests"
        assert diagnosis.retryable is True
        # An infrastructure diagnosis would take the retry-once path and search
        # nothing at all.
        assert diagnosis.infrastructure_related is False
        assert "gate passed" in diagnosis.concise_feedback
