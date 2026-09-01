"""Per-milestone objective rows written by a finished run."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from orchestra.cli.run_codeprojecteval_decomp import read_tuning_config
from orchestra.control.fast_loop.objectives import milestone_objectives


@dataclass
class _Status:
    value: str


@dataclass
class _Attempt:
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class _Sub:
    status: _Status
    attempts: list[_Attempt] = field(default_factory=list)


@dataclass
class _Usage:
    subtask_id: str
    prompt_tokens: int
    completion_tokens: int
    estimated_cost_usd: float | None = None


@dataclass
class _Candidate:
    harness_score: float | None
    furthest_stage: str = ""
    candidate_id: str | None = None
    status: str = ""
    behaviour_score: float | None = None


@dataclass
class _FastLoop:
    candidates: list[_Candidate] = field(default_factory=list)
    # Named as FastLoopState names it. The double used to invent
    # `winner_candidate_id`, so the tests agreed with each other about a field
    # the real state does not have and every repaired milestone was recorded as
    # having been won by the main path.
    selected_candidate_id: str | None = None


@dataclass
class _State:
    subtasks: dict[str, Any]
    backend_usage_records: list[_Usage] = field(default_factory=list)
    fast_loop_states: dict[str, _FastLoop] = field(default_factory=dict)


def test_each_milestone_gets_a_row_with_all_three_axes() -> None:
    state = _State(
        subtasks={"m1": _Sub(_Status("committed")), "m2": _Sub(_Status("failed"))},
        backend_usage_records=[
            _Usage("m1", 1000, 100, 0.02),
            _Usage("m1", 500, 50, 0.01),
            _Usage("m2", 2000, 200, 0.05),
        ],
        fast_loop_states={"m2": _FastLoop([_Candidate(0.4, "imports"), _Candidate(0.7, "tests")])},
    )

    rows = {row.milestone_id: row for row in milestone_objectives(state)}

    assert rows["m1"].gate_passed is True
    assert rows["m1"].total_tokens == 1650
    assert rows["m1"].estimated_cost_usd == 0.03
    # The failed milestone still reports how far its best candidate got, which
    # is the only thing distinguishing it from a milestone that produced nothing.
    assert rows["m2"].gate_passed is False
    assert rows["m2"].harness_score == 0.7
    assert rows["m2"].furthest_stage == "tests"


def test_a_repaired_milestone_is_credited_to_the_candidate_that_won() -> None:
    """On a tuning run this is the one thing the row exists to say."""
    state = _State(
        subtasks={"m1": _Sub(_Status("committed"))},
        fast_loop_states={
            "m1": _FastLoop(
                [_Candidate(1.0, "tests")], selected_candidate_id="cand_feedback"
            )
        },
    )

    assert milestone_objectives(state)[0].candidate_id == "cand_feedback"


def test_a_milestone_that_never_needed_repair_is_credited_to_the_main_path() -> None:
    state = _State(subtasks={"m1": _Sub(_Status("committed"))})

    assert milestone_objectives(state)[0].candidate_id == "main"


def test_the_stage_breakdown_travels_with_the_score() -> None:
    """A bare 0.62 says little; "contracts 12/19" says where to look."""
    breakdown = [
        {"stage": "imports", "passed_units": 10, "total_units": 10, "weight": 0.25},
        {"stage": "contracts", "passed_units": 12, "total_units": 19, "weight": 0.2},
    ]
    state = _State(
        subtasks={
            "m1": _Sub(
                _Status("failed"),
                attempts=[
                    _Attempt(
                        {
                            "harness_score": 0.62,
                            "furthest_stage": "contracts",
                            "harness_stages": breakdown,
                        }
                    )
                ],
            )
        }
    )

    row = milestone_objectives(state)[0]

    assert row.stages == breakdown
    assert row.to_dict()["stages"] == breakdown


def test_rows_are_written_even_when_tuning_is_off() -> None:
    """Otherwise a tuning loop has nothing to calibrate against."""
    state = _State(subtasks={"m1": _Sub(_Status("committed"))})

    rows = milestone_objectives(state)

    assert len(rows) == 1
    assert rows[0].harness_score is None
    assert rows[0].effective_score == 1.0


def test_the_graded_score_is_recorded_with_the_fast_loop_switched_off() -> None:
    """The main path records it on the attempt, not as a fast-loop candidate.

    Reading only candidates reported no score at all whenever the loop was off,
    which is every A/B run -- so the axis that separates two failures was blank
    on exactly the runs a tuning loop would be calibrated against.
    """
    state = _State(
        subtasks={
            "m1": _Sub(
                _Status("failed"),
                attempts=[_Attempt({"harness_score": 0.62, "furthest_stage": "imports"})],
            )
        },
        fast_loop_states={},
    )

    row = milestone_objectives(state)[0]

    assert row.harness_score == 0.62
    assert row.furthest_stage == "imports"
    assert row.gate_passed is False


def test_tuning_is_off_unless_a_config_asks_for_it() -> None:
    """A repair loop firing in one A/B arm would be measured as part of the arm."""
    tuning = read_tuning_config({"experiment": {}})

    assert tuning.candidates == 0
    assert tuning.weights.allow_cost_to_outrank_gate is False
    # Design search changes what a candidate is, so it must never arrive by
    # default in a run whose purpose is to measure something else.
    assert tuning.design_search is False
    assert tuning.playbook_search is False
    assert tuning.diagnosis.mode == "deterministic"


def test_the_shipped_codeprojecteval_config_keeps_tuning_off() -> None:
    import yaml

    config = yaml.safe_load(
        open("configs/experiments/codeprojecteval_decomp.yaml", encoding="utf-8")
    )
    tuning = read_tuning_config(config)

    assert tuning.candidates == 0
    assert tuning.weights.gate_weight == 1.0
    assert tuning.weights.harness_weight == 0.5


def test_the_pareto_axes_come_with_tolerances_whether_or_not_a_config_sets_them() -> None:
    tuning = read_tuning_config({"experiment": {}})
    assert set(tuning.pareto.epsilon) == {"quality", "cost", "stability"}
    assert tuning.pareto.epsilon["quality"] > 0.0


def test_a_config_can_widen_the_search_and_set_its_tolerances() -> None:
    tuning = read_tuning_config(
        {
            "experiment": {
                "tuning": {
                    "fast_loop_candidates": 3,
                    "design_search": True,
                    "pareto": {"rule": "balanced_knee", "epsilon": {"cost": 0.25}},
                }
            }
        }
    )
    assert tuning.candidates == 3
    assert tuning.design_search is True
    assert tuning.pareto.rule == "balanced_knee"
    assert tuning.pareto.epsilon["cost"] == 0.25
    # Unset axes keep their calibrated defaults rather than dropping to zero.
    assert tuning.pareto.epsilon["quality"] > 0.0


def test_playbook_search_and_design_search_cannot_share_a_config() -> None:
    with pytest.raises(ValueError, match="mutually exclusive"):
        read_tuning_config(
            {
                "experiment": {
                    "tuning": {"design_search": True, "playbook_search": True}
                }
            }
        )


def test_the_playbook_experiment_config_arms_the_new_generator() -> None:
    import yaml

    config = yaml.safe_load(
        open("configs/experiments/codeprojecteval_official_playbook.yaml", encoding="utf-8")
    )
    tuning = read_tuning_config(config)
    assert tuning.playbook_search is True
    assert tuning.design_search is False
    assert tuning.candidates == 3
    assert tuning.diagnosis.mode == "llm"


def test_an_unknown_selection_rule_is_refused_before_the_run_starts() -> None:
    with pytest.raises(ValueError, match="unknown fast-loop selection rule"):
        read_tuning_config(
            {"experiment": {"tuning": {"pareto": {"rule": "vibes"}}}}
        )


def test_a_discarded_candidate_does_not_lend_its_scores_to_the_winner() -> None:
    """The row describes what was committed, not the best number on the list.

    A candidate can top both axes and still be discarded — failing the
    milestone's contracts scores its quality at zero however good its behaviour
    was. Reporting that candidate's numbers beside the *selected* candidate's id
    overstates the milestone, and does so in the direction of a design the
    selector deliberately refused.
    """
    winner = _Candidate(0.9625, "spec_tests")
    winner.candidate_id = "cand_feedback"
    winner.status = "committed"
    winner.behaviour_score = 0.90625
    loser = _Candidate(0.963889, "spec_tests")
    loser.candidate_id = "cand_pb_tf_q_improve_after_gate"
    loser.status = "discarded"
    loser.behaviour_score = 0.9375

    state = _State(
        subtasks={"m1": _Sub(_Status("committed"))},
        fast_loop_states={
            "m1": _FastLoop([winner, loser], selected_candidate_id="cand_feedback")
        },
    )
    row = milestone_objectives(state)[0]

    assert row.candidate_id == "cand_feedback"
    assert row.harness_score == 0.9625
    assert row.behaviour == 0.90625
