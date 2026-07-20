"""M6 preference-conditioned selector tests."""

from __future__ import annotations

from orchestra.control.pareto.schemas import (
    ObjectiveDirection,
    ObjectiveSource,
    ObjectiveValue,
    ParetoEvaluationKind,
    ParetoObjectiveVector,
    ParetoOrchestraCandidate,
    PreferenceProfile,
)
from orchestra.control.pareto.selector import DeterministicParetoSelector

OBJ = {
    "quality": ObjectiveDirection.MAXIMIZE,
    "cost": ObjectiveDirection.MINIMIZE,
    "latency": ObjectiveDirection.MINIMIZE,
    "risk": ObjectiveDirection.MINIMIZE,
}


def _cand(cid: str, *, quality, cost, latency, risk, edits=0) -> ParetoOrchestraCandidate:
    values = {
        "quality": ObjectiveValue(
            value=quality, source=ObjectiveSource.ESTIMATED, available=True
        ),
        "cost": ObjectiveValue(value=cost, source=ObjectiveSource.ESTIMATED, available=True),
        "latency": ObjectiveValue(
            value=latency, source=ObjectiveSource.ESTIMATED, available=True
        ),
        "risk": ObjectiveValue(value=risk, source=ObjectiveSource.ESTIMATED, available=True),
    }
    return ParetoOrchestraCandidate(
        candidate_id=cid,
        content_hash=cid,
        edit_signature=cid,
        context_id="ctx",
        edits=[object()] * edits,
        global_candidate=None,
        objectives=ParetoObjectiveVector(
            values=values, evaluation_kind=ParetoEvaluationKind.ESTIMATED
        ),
        communication_overhead=float(edits),
    )


FRONTIER = [
    _cand("hq", quality=1.0, cost=10.0, latency=5.0, risk=0.2, edits=2),
    _cand("lc", quality=0.6, cost=1.0, latency=4.0, risk=0.3, edits=1),
    _cand("fast", quality=0.7, cost=4.0, latency=0.5, risk=0.8, edits=1),
    _cand("robust", quality=0.75, cost=5.0, latency=3.0, risk=0.05, edits=1),
]


def test_quality_first_profile():
    sel = DeterministicParetoSelector()
    chosen = sel.select(
        FRONTIER, PreferenceProfile(profile_id="quality_first"), OBJ
    )
    assert chosen is not None
    assert chosen.content_hash == "hq"


def test_cost_cap_profile():
    sel = DeterministicParetoSelector()
    chosen = sel.select(
        FRONTIER,
        PreferenceProfile(profile_id="cost_capped_quality", maximum_cost_usd=2.0),
        OBJ,
    )
    assert chosen is not None
    assert chosen.objectives.values["cost"].value <= 2.0


def test_latency_cap_profile():
    sel = DeterministicParetoSelector()
    chosen = sel.select(
        FRONTIER,
        PreferenceProfile(
            profile_id="latency_capped_quality", maximum_latency_seconds=1.0
        ),
        OBJ,
    )
    assert chosen is not None
    assert chosen.objectives.values["latency"].value <= 1.0


def test_robustness_profile():
    sel = DeterministicParetoSelector()
    chosen = sel.select(
        FRONTIER, PreferenceProfile(profile_id="robustness_first"), OBJ
    )
    assert chosen is not None
    assert chosen.content_hash == "robust"


def test_balanced_knee_is_deterministic():
    sel = DeterministicParetoSelector()
    a = sel.select(FRONTIER, PreferenceProfile(profile_id="balanced_knee"), OBJ)
    b = sel.select(FRONTIER, PreferenceProfile(profile_id="balanced_knee"), OBJ)
    assert a is not None and b is not None
    assert a.content_hash == b.content_hash


def test_preference_filter_can_return_no_candidate():
    sel = DeterministicParetoSelector()
    chosen = sel.select(
        FRONTIER,
        PreferenceProfile(
            profile_id="quality_first",
            minimum_quality=0.99,
            maximum_cost_usd=0.5,
        ),
        OBJ,
    )
    assert chosen is None
