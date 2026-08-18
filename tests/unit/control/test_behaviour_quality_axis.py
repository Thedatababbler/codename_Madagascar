"""Quality has to be read off the stage that can still move.

A CodeProjectEval milestone that commits has passed compile, imports and contracts
outright — every one ever recorded did — and those stages hold 0.6 to 0.7 of the
graded score's weight. So the blended score is pinned into a narrow band near the
top whatever the code actually does, and two designs whose behaviour differs
materially arrive at selection looking like a tie (EXP-20260810-05).

That has two consequences, both tested here: the Pareto quality axis cannot rank
designs, and a "passed but poor" trigger threshold has no value that both fires on
bad milestones and spares good ones.
"""

from __future__ import annotations

from orchestra.control.fast_loop.objectives import MilestoneObjective
from orchestra.control.fast_loop.pareto import (
    QUALITY,
    ParetoSelectionConfig,
    discriminating_quality,
    frontier,
    objective_vector,
)
from orchestra.control.fast_loop.quality_trigger import QualityTrigger
from orchestra.control.fast_loop.schemas import (
    CandidateRecord,
    CandidateStatus,
    CostRecord,
)
from orchestra.harness.progress import behaviour_score

#: The weights a committed integration milestone is graded under, and the structural
#: stages a committed one always passes. Kept explicit so the arithmetic below is
#: checkable rather than asserted.
STRUCTURAL_WEIGHT = 0.1 + 0.15 + 0.15 + 0.3  # compile, imports, contracts, tests
SPEC_WEIGHT = 0.3


def _blended(spec_fraction: float) -> float:
    return STRUCTURAL_WEIGHT + SPEC_WEIGHT * spec_fraction


def _candidate(
    cid: str,
    *,
    spec_passed: int,
    spec_total: int = 58,
    cost: float,
    status: CandidateStatus = CandidateStatus.VALID,
) -> CandidateRecord:
    fraction = spec_passed / spec_total
    return CandidateRecord(
        candidate_id=cid,
        attempt_id=2,
        graph_hash="h",
        parent_graph_hash="h",
        edits=[],
        status=status,
        quality_score=1.0 if status is CandidateStatus.VALID else 0.0,
        harness_score=_blended(fraction),
        behaviour_score=fraction,
        furthest_stage="spec_tests",
        cost=CostRecord(prompt_tokens=1000, completion_tokens=100, estimated_cost_usd=cost),
    )


#: Seven tests no candidate passed, as observed. They lower every score equally.
_ALWAYS_FAIL = {f"always_fail_{i}" for i in range(7)}


def _with_failures(
    cid: str,
    *,
    total: int,
    failed: set[str],
    cost: float = 1.0,
    status: CandidateStatus = CandidateStatus.VALID,
) -> CandidateRecord:
    return CandidateRecord(
        candidate_id=cid,
        attempt_id=2,
        graph_hash="h",
        parent_graph_hash="h",
        edits=[],
        status=status,
        quality_score=1.0 if status is CandidateStatus.VALID else 0.0,
        harness_score=_blended((total - len(failed)) / total),
        behaviour_score=(total - len(failed)) / total,
        behaviour_failures=sorted(failed),
        behaviour_total=total,
        furthest_stage="spec_tests",
        cost=CostRecord(prompt_tokens=1000, completion_tokens=100, estimated_cost_usd=cost),
    )


class TestReadingBehaviourOffTheStages:
    def test_the_authored_suite_is_preferred_over_the_visible_one(self):
        """Both measure behaviour; the authored suite was written for this scope."""
        assert behaviour_score(
            [
                {"stage": "tests", "passed_units": 9, "total_units": 9},
                {"stage": "spec_tests", "passed_units": 49, "total_units": 58},
            ]
        ) == 49 / 58

    def test_the_visible_suite_is_used_when_nothing_was_authored(self):
        assert behaviour_score(
            [
                {"stage": "compile", "passed_units": 1, "total_units": 1},
                {"stage": "tests", "passed_units": 7, "total_units": 9},
            ]
        ) == 7 / 9

    def test_structural_stages_alone_report_nothing(self):
        """Not zero. A milestone with no behavioural stage was not measured badly."""
        assert (
            behaviour_score(
                [
                    {"stage": "compile", "passed_units": 1, "total_units": 1},
                    {"stage": "imports", "passed_units": 17, "total_units": 17},
                    {"stage": "contracts", "passed_units": 18, "total_units": 18},
                ]
            )
            is None
        )

    def test_an_empty_suite_reports_nothing_rather_than_zero(self):
        """Otherwise authoring no tests would be the worst possible design."""
        empty = [{"stage": "spec_tests", "passed_units": 0, "total_units": 0}]

        assert behaviour_score(empty) is None

    def test_stage_objects_are_read_as_well_as_mappings(self):
        """The scheduler holds model objects; the objective record holds dicts."""

        class Stage:
            def __init__(self, stage, passed_units, total_units):
                self.stage = stage
                self.passed_units = passed_units
                self.total_units = total_units

        assert behaviour_score([Stage("spec_tests", 46, 58)]) == 46 / 58


class TestTheAxisCanNowSeparateDesigns:
    def test_the_blend_would_have_called_the_observed_spread_a_tie(self):
        """The arithmetic that motivates this: 0.069 of behaviour is 0.021 blended.

        The recorded imapclient candidates spanned 46/58 to 50/58 on the authored
        suite. Blended, that is under the 0.02 quality epsilon, so the frontier
        would report them as indistinguishable and fall through to price.
        """
        low, high = 46 / 58, 50 / 58
        assert high - low > 0.06
        assert _blended(high) - _blended(low) < 0.022

    def test_quality_reads_behaviour_not_the_blend(self):
        cand = _candidate("c", spec_passed=46, cost=1.0)

        assert objective_vector(cand)[QUALITY]["value"] == 46 / 58

    def test_two_designs_separated_on_behaviour_both_survive(self):
        """Better-but-dearer against worse-but-cheaper is a real trade-off.

        Under the blended score the quality gap falls inside epsilon, the dearer
        candidate is dominated on both axes, and the frontier collapses to the
        cheap one — the degeneracy EXP-20260810-03 recorded.
        """
        cfg = ParetoSelectionConfig()
        good = _candidate("good", spec_passed=50, cost=1.4)
        cheap = _candidate("cheap", spec_passed=46, cost=1.0)

        assert {c.candidate_id for c in frontier([good, cheap], cfg)} == {"good", "cheap"}

    def test_a_one_test_difference_is_still_a_tie(self):
        """Epsilon is doing its job: 1/58 is 0.017, inside the 0.02 tolerance.

        The axis is not supposed to chase single flipped tests, which are as likely
        to be noise in an authored suite as evidence about the design.
        """
        cfg = ParetoSelectionConfig()
        better = _candidate("better", spec_passed=50, cost=1.4)
        cheaper = _candidate("cheaper", spec_passed=49, cost=1.0)

        assert {c.candidate_id for c in frontier([better, cheaper], cfg)} == {"cheaper"}

    def test_a_milestone_with_no_behavioural_stage_still_ranks(self):
        """Backward compatibility: absent behaviour falls back to the blend."""
        cand = CandidateRecord(
            candidate_id="legacy",
            attempt_id=1,
            graph_hash="h",
            parent_graph_hash="h",
            edits=[],
            status=CandidateStatus.VALID,
            quality_score=1.0,
            harness_score=0.62,
            cost=CostRecord(prompt_tokens=10, completion_tokens=1, estimated_cost_usd=0.5),
        )

        vector = objective_vector(cand)
        assert vector[QUALITY]["available"] is True
        assert vector[QUALITY]["value"] == 0.62


class TestDroppingTheTestsThatCannotRank:
    """Constants in a score consume its range without carrying information.

    The recorded imapclient search graded three candidates against 58 authored
    tests, of which 43 passed for all three and 7 failed for all three. So 50 units
    of the axis were fixed and the 8 that moved were reported at 8/58 of their size,
    which is how a real difference ended up inside epsilon.
    """

    def test_the_constant_part_is_removed(self):
        pool = [
            _with_failures("a", total=58, failed=_ALWAYS_FAIL | {"t_x"}),
            _with_failures("b", total=58, failed=_ALWAYS_FAIL | {"t_y"}),
            _with_failures("c", total=58, failed=_ALWAYS_FAIL),
        ]

        refined = discriminating_quality(pool)

        # Two tests vary (t_x, t_y); the seven everyone fails do not.
        assert refined == {"a": 0.5, "b": 0.5, "c": 1.0}

    def test_the_difference_now_clears_epsilon(self):
        """The point of the exercise: 1/58 is a tie, 1/2 of what moved is not."""
        pool = [
            _with_failures("worse", total=58, failed=_ALWAYS_FAIL | {"t_x"}, cost=1.0),
            _with_failures("better", total=58, failed=_ALWAYS_FAIL, cost=1.4),
        ]
        raw = [c.behaviour_score for c in pool]
        assert max(raw) - min(raw) < ParetoSelectionConfig().epsilon["quality"]

        for cand in pool:
            cand.comparable_quality = discriminating_quality(pool)[cand.candidate_id]
        surviving = {c.candidate_id for c in frontier(pool, ParetoSelectionConfig())}

        assert surviving == {"worse", "better"}

    def test_a_pool_that_failed_identically_is_a_tie(self):
        """Not a difference invented from rounding: they did the same thing."""
        pool = [
            _with_failures("a", total=58, failed=_ALWAYS_FAIL),
            _with_failures("b", total=58, failed=_ALWAYS_FAIL),
        ]

        assert discriminating_quality(pool) == {"a": 1.0, "b": 1.0}

    def test_suites_of_different_sizes_are_not_compared(self):
        """Different totals mean different yardsticks, which is not a frontier."""
        pool = [
            _with_failures("a", total=58, failed={"t_1"}),
            _with_failures("b", total=40, failed={"t_1"}),
        ]

        assert discriminating_quality(pool) == {}

    def test_a_pool_of_one_is_not_a_comparison(self):
        assert discriminating_quality([_with_failures("a", total=58, failed={"t_1"})]) == {}

    def test_unnamed_failures_fall_back_rather_than_guessing(self):
        """A stage reporting counts but no identities cannot be split up.

        Treating the missing list as "failed nothing" would score a candidate that
        failed twelve tests as perfect, which is worse than not refining at all.
        """
        opaque = _with_failures("opaque", total=58, failed=set())
        opaque.behaviour_score = 46 / 58
        pool = [opaque, _with_failures("named", total=58, failed={"t_1"})]

        assert discriminating_quality(pool) == {}

    def test_candidates_that_never_ran_are_excluded(self):
        pool = [
            _with_failures("ran", total=58, failed={"t_1"}),
            _with_failures(
                "rejected", total=58, failed={"t_2"}, status=CandidateStatus.REJECTED
            ),
        ]

        assert discriminating_quality(pool) == {}

    def test_a_failing_candidate_still_takes_part(self):
        """The axis ranks work; the gate is enforced separately in selection."""
        pool = [
            _with_failures("passed", total=58, failed={"t_1"}),
            _with_failures(
                "failed_gate",
                total=58,
                failed={"t_1", "t_2"},
                status=CandidateStatus.HARNESS_FAILED,
            ),
        ]

        assert discriminating_quality(pool) == {"passed": 1.0, "failed_gate": 0.0}


class TestTheTriggerThresholdIsNowReachable:
    def test_no_blended_threshold_could_separate_the_recorded_runs(self):
        """Why the trigger reads behaviour: on the blend there is no usable value.

        A threshold below the band never fires, one above it fires on everything,
        and the band is 0.02 wide — narrower than the resolution anyone would set a
        threshold to.
        """
        band = [_blended(f / 58) for f in (46, 49, 50)]
        assert min(band) > 0.9
        assert max(band) - min(band) < 0.025

        never = QualityTrigger(enabled=True, min_score=0.8)
        assert [never.fires(gate_passed=True, behaviour_score=b) for b in band] == [
            False,
            False,
            False,
        ]

    def test_behaviour_gives_the_threshold_somewhere_to_sit(self):
        trigger = QualityTrigger(enabled=True, min_score=0.9)
        fired = [
            trigger.fires(gate_passed=True, behaviour_score=f / 58) for f in (46, 49, 50)
        ]

        assert fired == [True, True, True]

    def test_a_milestone_that_behaves_well_is_spared(self):
        trigger = QualityTrigger(enabled=True, min_score=0.9)

        assert trigger.fires(gate_passed=True, behaviour_score=0.95) is False


class TestTheRecordReportsBoth:
    def test_behaviour_is_derived_from_the_stage_breakdown(self):
        objective = MilestoneObjective(
            milestone_id="m",
            candidate_id="main",
            gate_passed=True,
            harness_score=1.0,
            stages=[
                {"stage": "compile", "passed_units": 1, "total_units": 1, "weight": 0.1},
                {"stage": "spec_tests", "passed_units": 49, "total_units": 58, "weight": 0.3},
            ],
        )

        assert objective.behaviour_score == 49 / 58
        assert objective.to_dict()["behaviour_score"] == 49 / 58
        assert objective.to_dict()["harness_score"] == 1.0

    def test_a_winning_candidates_behaviour_overrides_the_attempts_stages(self):
        """A candidate carries no stage breakdown, so it has to travel explicitly."""
        objective = MilestoneObjective(
            milestone_id="m",
            candidate_id="cand_add_contract_author",
            gate_passed=True,
            harness_score=0.95,
            stages=[{"stage": "spec_tests", "passed_units": 46, "total_units": 58}],
            behaviour=50 / 58,
        )

        assert objective.behaviour_score == 50 / 58

    def test_a_milestone_without_a_behavioural_stage_reports_none(self):
        objective = MilestoneObjective(
            milestone_id="m",
            candidate_id="main",
            gate_passed=True,
            harness_score=1.0,
            stages=[{"stage": "imports", "passed_units": 17, "total_units": 17}],
        )

        assert objective.behaviour_score is None
