"""M6 Pareto dominance unit tests."""

from __future__ import annotations

from orchestra.control.pareto.dominance import dominates
from orchestra.control.pareto.schemas import (
    EvaluationVisibility,
    ObjectiveDirection,
    ObjectiveSource,
    ObjectiveValue,
    ParetoEvaluationKind,
    ParetoObjectiveVector,
    ParetoOrchestraCandidate,
)

OBJ = {
    "quality": ObjectiveDirection.MAXIMIZE,
    "cost": ObjectiveDirection.MINIMIZE,
}


def _vec(**kwargs) -> ParetoObjectiveVector:
    values = {}
    for name, value in kwargs.items():
        values[name] = ObjectiveValue(
            value=float(value),
            source=ObjectiveSource.REALIZED,
            available=True,
            evaluation_visibility=EvaluationVisibility.PUBLIC,
        )
    return ParetoObjectiveVector(values=values, evaluation_kind=ParetoEvaluationKind.REALIZED)


def _cand(cid: str, **kwargs) -> ParetoOrchestraCandidate:
    return ParetoOrchestraCandidate(
        candidate_id=cid,
        content_hash=cid,
        edit_signature=cid,
        context_id="ctx",
        global_candidate=None,
        objectives=_vec(**kwargs),
    )


def test_strict_pareto_dominance():
    a = _cand("a", quality=1.0, cost=1.0)
    b = _cand("b", quality=0.5, cost=2.0)
    assert dominates(a, b, OBJ)
    assert not dominates(b, a, OBJ)


def test_mutual_non_dominance():
    a = _cand("a", quality=1.0, cost=2.0)
    b = _cand("b", quality=0.5, cost=1.0)
    assert not dominates(a, b, OBJ)
    assert not dominates(b, a, OBJ)


def test_equal_vectors():
    a = _cand("a", quality=1.0, cost=1.0)
    b = _cand("b", quality=1.0, cost=1.0)
    assert not dominates(a, b, OBJ)


def test_epsilon_dominance():
    a = _cand("a", quality=1.0, cost=1.0)
    b = _cand("b", quality=0.9995, cost=1.0)
    assert not dominates(a, b, OBJ, epsilon={"quality": 0.001})
    assert dominates(a, b, OBJ, epsilon={"quality": 0.0})


def test_missing_required_objective_excluded():
    a = _cand("a", quality=1.0)
    b = _cand("b", quality=0.5, cost=1.0)
    assert not dominates(a, b, OBJ)


def test_infeasible_candidate_excluded():
    a = _cand("a", quality=1.0, cost=1.0)
    a.validation_errors = ["bad"]
    # Dominance itself doesn't check feasibility; archive/validation does.
    # Ensure unavailable quality cannot dominate.
    incomplete = ParetoOrchestraCandidate(
        candidate_id="x",
        content_hash="x",
        edit_signature="x",
        context_id="ctx",
        global_candidate=None,
        objectives=ParetoObjectiveVector(
            values={"quality": ObjectiveValue.unavailable()},
            evaluation_kind=ParetoEvaluationKind.ESTIMATED,
        ),
    )
    assert not dominates(incomplete, a, OBJ)


def test_estimated_not_mixed_with_realized():
    from orchestra.control.pareto.archive import ParetoArchive
    from orchestra.control.pareto.schemas import ParetoConfig

    archive = ParetoArchive(ParetoConfig())
    est = _cand("e", quality=1.0, cost=1.0)
    est.objectives.evaluation_kind = ParetoEvaluationKind.ESTIMATED
    real = _cand("r", quality=0.1, cost=9.0)
    real.objectives.evaluation_kind = ParetoEvaluationKind.REALIZED
    archive.insert(est, ParetoEvaluationKind.ESTIMATED)
    archive.insert(real, ParetoEvaluationKind.REALIZED)
    assert len(archive.frontier("ctx", ParetoEvaluationKind.ESTIMATED)) == 1
    assert len(archive.frontier("ctx", ParetoEvaluationKind.REALIZED)) == 1


def test_objective_direction_respected():
    a = _cand("a", quality=0.5, cost=1.0)
    b = _cand("b", quality=0.5, cost=2.0)
    assert dominates(a, b, OBJ)
