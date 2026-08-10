"""Frontier construction and the choice of one point off it.

The property that matters is not "which candidate wins" -- a scalar selector also
answers that -- but that two candidates trading quality against cost both survive
to be reported. That is the difference between a search and a ranking, and it is
the thing a degenerate milestone will show by violating.
"""

from __future__ import annotations

from orchestra.control.fast_loop.pareto import (
    ParetoSelectionConfig,
    SelectionRule,
    frontier,
    objective_vector,
    select_from_frontier,
)
from orchestra.control.fast_loop.schemas import (
    CandidateRecord,
    CandidateStatus,
    CostRecord,
    FastLoopBudget,
    PromptFeedbackEdit,
    StabilityIncident,
)
from orchestra.control.fast_loop.selector import ParetoCandidateSelector
from orchestra.control.task_state import SubtaskFailureReason

BUDGET = FastLoopBudget(max_candidates=4, max_total_backend_calls=16)


def _candidate(
    candidate_id: str,
    *,
    score: float | None,
    usd: float,
    passed: bool = False,
    incidents: int = 0,
    tokens: int = 100_000,
    failure: SubtaskFailureReason | None = None,
) -> CandidateRecord:
    return CandidateRecord(
        candidate_id=candidate_id,
        attempt_id=1,
        graph_hash=f"hash_{candidate_id}",
        parent_graph_hash="parent",
        edits=[PromptFeedbackEdit(node_id="a", feedback="f")],
        status=CandidateStatus.VALID if passed else CandidateStatus.HARNESS_FAILED,
        quality_score=1.0 if passed else 0.0,
        harness_score=score,
        cost=CostRecord(
            prompt_tokens=tokens, completion_tokens=0, estimated_cost_usd=usd, backend_calls=1
        ),
        stability_incidents=[
            StabilityIncident(kind="infra_retry", message="retry") for _ in range(incidents)
        ],
        failure_reason=failure,
    )


def _ids(records: list[CandidateRecord]) -> set[str]:
    return {r.candidate_id for r in records}


class TestFrontier:
    def test_a_dominated_candidate_is_dropped(self) -> None:
        good = _candidate("good", score=0.9, usd=1.0)
        worse = _candidate("worse", score=0.5, usd=2.0)
        assert _ids(frontier([good, worse])) == {"good"}

    def test_a_cheaper_worse_candidate_survives_as_a_trade_off(self) -> None:
        strong = _candidate("strong", score=0.9, usd=3.0)
        cheap = _candidate("cheap", score=0.6, usd=0.5)
        # Both must survive: this is the case a weighted sum cannot express, and
        # the reason the frontier is worth building.
        assert _ids(frontier([strong, cheap])) == {"strong", "cheap"}

    def test_candidates_that_never_ran_are_not_on_the_frontier(self) -> None:
        ran = _candidate("ran", score=0.6, usd=1.0)
        rejected = _candidate("rejected", score=None, usd=0.0, tokens=0)
        rejected = rejected.model_copy(update={"status": CandidateStatus.REJECTED})
        assert _ids(frontier([ran, rejected])) == {"ran"}

    def test_an_unpriced_candidate_cannot_look_free(self) -> None:
        priced = _candidate("priced", score=0.6, usd=1.0)
        unpriced = _candidate("unpriced", score=0.9, usd=0.0, tokens=500_000)
        # Cost defaults to 0.0 rather than None, so without the tokens check this
        # candidate would be both better and free, and would dominate outright.
        vector = objective_vector(unpriced)
        assert vector["cost"]["available"] is False
        assert _ids(frontier([priced, unpriced])) == {"priced", "unpriced"}

    def test_stability_separates_candidates_that_agree_on_the_rest(self) -> None:
        clean = _candidate("clean", score=0.7, usd=1.0, incidents=0)
        flaky = _candidate("flaky", score=0.7, usd=1.0, incidents=3)
        assert _ids(frontier([clean, flaky])) == {"clean"}

    def test_a_failing_candidate_cannot_push_a_passing_one_off_the_frontier(self) -> None:
        """Failing the gate is a disqualification, not a point on a trade-off curve.

        A candidate that abandons the gate can score high and spend little, which
        dominates the passing work on both axes. Once the passing point is gone
        selection has nothing safe left to prefer, so `require_gate_pass` stops
        protecting anything — the guard has to be in dominance.
        """
        passing = _candidate("passing", score=0.55, usd=1.0, passed=True)
        rogue = _candidate("rogue", score=0.95, usd=0.2, passed=False)

        assert _ids(frontier([passing, rogue])) == {"passing", "rogue"}
        winner = select_from_frontier(frontier([passing, rogue]))
        assert winner is not None
        assert winner.candidate_id == "passing"

    def test_a_passing_candidate_may_still_dominate_a_failing_one(self) -> None:
        """The asymmetry is deliberate: better on every axis that matters."""
        passing = _candidate("passing", score=0.95, usd=0.5, passed=True)
        failing = _candidate("failing", score=0.40, usd=2.0, passed=False)

        assert _ids(frontier([passing, failing])) == {"passing"}

    def test_an_all_failing_set_is_unaffected_by_the_rule(self) -> None:
        """The repair path legitimately compares failures against each other."""
        near = _candidate("near", score=0.8, usd=1.0, passed=False)
        far = _candidate("far", score=0.2, usd=2.0, passed=False)

        assert _ids(frontier([near, far])) == {"near"}


class TestEpsilon:
    def test_a_difference_below_the_tolerance_is_a_tie(self) -> None:
        a = _candidate("a", score=0.700, usd=1.50)
        b = _candidate("b", score=0.705, usd=1.50)
        # 0.005 of harness score is finer than one unit of the coarsest stage;
        # letting it decide would be reading noise as a result.
        assert _ids(frontier([a, b])) == {"a", "b"}

    def test_a_difference_above_the_tolerance_still_decides(self) -> None:
        a = _candidate("a", score=0.70, usd=1.50)
        b = _candidate("b", score=0.80, usd=1.50)
        assert _ids(frontier([a, b])) == {"b"}

    def test_cost_within_a_few_cents_is_a_tie(self) -> None:
        a = _candidate("a", score=0.70, usd=1.50)
        b = _candidate("b", score=0.70, usd=1.53)
        assert _ids(frontier([a, b])) == {"a", "b"}

    def test_tolerances_are_configurable(self) -> None:
        config = ParetoSelectionConfig.from_mapping({"epsilon": {"quality": 0.0}})
        a = _candidate("a", score=0.700, usd=1.50)
        b = _candidate("b", score=0.705, usd=1.50)
        assert _ids(frontier([a, b], config)) == {"b"}


class TestSelection:
    def test_a_failing_candidate_is_never_committed_over_a_passing_one(self) -> None:
        passing = _candidate("passing", score=1.0, usd=4.0, passed=True)
        cheap_failure = _candidate("cheap", score=0.3, usd=0.4)
        points = frontier([passing, cheap_failure])
        assert _ids(points) == {"passing", "cheap"}
        # The frontier stays honest; the commit does not.
        assert select_from_frontier(points).candidate_id == "passing"

    def test_quality_first_prefers_the_better_score_over_the_cheaper_run(self) -> None:
        strong = _candidate("strong", score=0.9, usd=3.0, passed=True)
        cheap = _candidate("cheap", score=0.7, usd=0.5, passed=True)
        chosen = select_from_frontier([strong, cheap])
        assert chosen.candidate_id == "strong"

    def test_the_knee_rule_can_prefer_a_cheaper_near_miss(self) -> None:
        strong = _candidate("strong", score=0.90, usd=9.0, passed=True)
        cheap = _candidate("cheap", score=0.88, usd=0.5, passed=True)
        config = ParetoSelectionConfig.from_mapping({"rule": SelectionRule.BALANCED_KNEE})
        assert select_from_frontier([strong, cheap], config).candidate_id == "cheap"

    def test_selection_is_deterministic_under_a_full_tie(self) -> None:
        first = _candidate("aaa", score=0.7, usd=1.0, passed=True)
        second = _candidate("bbb", score=0.7, usd=1.0, passed=True)
        assert select_from_frontier([first, second]).candidate_id == "aaa"
        assert select_from_frontier([second, first]).candidate_id == "aaa"


class TestSelectorIntegration:
    def test_the_selector_records_the_frontier_it_built(self) -> None:
        selector = ParetoCandidateSelector()
        candidates = [
            _candidate("strong", score=0.9, usd=3.0, passed=True),
            _candidate("cheap", score=0.6, usd=0.5, passed=True),
            _candidate("dominated", score=0.5, usd=4.0, passed=True),
        ]
        winner = selector.select(candidates, BUDGET)
        assert winner is not None and winner.candidate_id == "strong"
        assert set(selector.last_frontier) == {"strong", "cheap"}
        assert selector.last_rule == SelectionRule.QUALITY_FIRST

    def test_an_unmeasurable_candidate_is_still_committed(self) -> None:
        # Its axes are unknown, so nothing can dominate it and nothing it can
        # dominate. Incomparability keeps it on the frontier rather than
        # eliminating it, which is what stops a missing price from failing a
        # milestone that did in fact pass its gate.
        selector = ParetoCandidateSelector()
        candidates = [_candidate("a", score=None, usd=0.0, tokens=500_000, passed=True)]
        winner = selector.select(candidates, BUDGET)
        assert winner is not None and winner.candidate_id == "a"
        assert selector.last_frontier == ["a"]

    def test_nothing_is_committed_when_no_candidate_ran(self) -> None:
        selector = ParetoCandidateSelector()
        rejected = _candidate("r", score=None, usd=0.0, tokens=0).model_copy(
            update={"status": CandidateStatus.REJECTED}
        )
        assert selector.select([rejected], BUDGET) is None
        assert selector.last_rule == "scalar_fallback"

    def test_an_infrastructure_failure_is_not_committed_as_a_design_win(self) -> None:
        selector = ParetoCandidateSelector()
        candidates = [
            _candidate(
                "infra",
                score=0.9,
                usd=0.2,
                passed=True,
                failure=SubtaskFailureReason.INFRA,
            ),
            _candidate("real", score=0.8, usd=2.0, passed=True),
        ]
        winner = selector.select(candidates, BUDGET)
        assert winner is not None and winner.candidate_id == "real"
