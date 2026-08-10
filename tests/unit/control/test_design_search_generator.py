"""The generator has to be able to fill the budget it is given.

The shipped generator could not: three drafts existed, one needed a second model
in the pool and one was disabled by default, so the effective candidate count was
two no matter how high ``max_candidates`` was set. Raising the budget without
fixing that would have bought a wider search on paper only.
"""

from __future__ import annotations

import pytest

from orchestra.control.fast_loop.candidate_generator import (
    DesignSearchCandidateGenerator,
)
from orchestra.control.fast_loop.schemas import (
    FailureDiagnosis,
    FastLoopBudget,
)
from orchestra.control.task_state import SubtaskFailureReason

from milestone_subgraph import FIXER, REVIEWER, review_then_fix_graph as _graph


def _diagnosis(stage: str = "contracts") -> FailureDiagnosis:
    return FailureDiagnosis(
        reason=SubtaskFailureReason.HARNESS,
        retryable=True,
        concise_feedback="FAIL milestone contracts: missing imapclient.exceptions.IMAPError",
        primary_failed_node_id=FIXER,
        failed_node_ids=[FIXER],
        recommended_edit_types=["prompt_feedback", "budget_adjustment"],
        furthest_stage=stage,
    )


def _generate(*, candidates: int, stage: str = "contracts"):
    return DesignSearchCandidateGenerator().generate(
        graph=_graph(),
        diagnosis=_diagnosis(stage),
        budget=FastLoopBudget(max_candidates=candidates, max_total_backend_calls=99),
        capabilities={},
    )


def test_a_budget_of_four_yields_four_distinct_designs() -> None:
    built = _generate(candidates=4)
    assert len(built) == 4
    assert len({c.candidate_id for c in built}) == 4
    # Four *designs*, not four copies: identical graphs would make the frontier
    # a set of duplicate points.
    assert len({c.graph.content_hash for c in built}) == 4


def test_each_candidate_differs_from_the_parent_by_one_design_edit() -> None:
    built = _generate(candidates=4)
    for candidate in built:
        design = [
            e
            for e in candidate.edits
            if e.type not in {"prompt_feedback", "session_policy"}
        ]
        assert len(design) <= 1, f"{candidate.candidate_id} bundles {design}"


def test_the_first_candidate_changes_nothing_but_the_feedback() -> None:
    built = _generate(candidates=4)
    anchor = built[0]
    assert anchor.candidate_id == "cand_feedback"
    assert {e.type for e in anchor.edits} == {"prompt_feedback", "session_policy"}


def test_the_added_role_follows_the_stage_that_failed() -> None:
    by_stage = {
        "imports": "dependency_resolver",
        "contracts": "contract_author",
        "tests": "test_driven_implementer",
    }
    for stage, role_id in by_stage.items():
        built = _generate(candidates=2, stage=stage)
        assert any(f"cand_add_{role_id}" == c.candidate_id for c in built), stage


def test_a_read_only_agent_becomes_a_cheaper_candidate() -> None:
    built = _generate(candidates=6)
    dropper = next(c for c in built if c.candidate_id.startswith("cand_drop_"))
    assert not any(n.node_id == REVIEWER for n in dropper.graph.nodes)


def test_an_unknown_stage_still_produces_a_repair_role() -> None:
    built = _generate(candidates=2, stage="")
    assert any(c.candidate_id == "cand_add_gate_repairer" for c in built)


def test_infrastructure_failures_are_not_searched() -> None:
    diagnosis = _diagnosis().model_copy(
        update={"infrastructure_related": True, "reason": SubtaskFailureReason.INFRA}
    )
    built = DesignSearchCandidateGenerator().generate(
        graph=_graph(),
        diagnosis=diagnosis,
        budget=FastLoopBudget(max_candidates=4, max_total_backend_calls=99),
        capabilities={},
    )
    # A provider outage says nothing about the design; paying to vary the design
    # in response would be measuring noise.
    assert built == []


@pytest.mark.parametrize("candidates", [0, 1, 2, 3, 4, 6, 12])
def test_the_generator_never_exceeds_its_budget(candidates: int) -> None:
    built = _generate(candidates=candidates)
    assert len(built) <= candidates
