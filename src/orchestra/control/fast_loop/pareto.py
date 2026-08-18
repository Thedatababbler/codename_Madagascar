"""Pareto search over one milestone's subgraph designs.

The fast loop's job, per §15 and §19 of the design document, is to search the
design of a *single* milestone and keep the set of designs that trade off against
each other rather than collapsing them into one number. What shipped instead was
a weighted scalar (`TuningWeights.utility`), which always returns exactly one
winner and can never report that two designs are incomparable -- so the fact
that the frontier of a real milestone might be degenerate was unobservable.

Three axes, per the archive:

* **quality** (maximize) -- the behavioural slice of the candidate's own graded
  acceptance score, falling back to the blended score when the harness reports no
  behavioural stage. Never the held-out suite: that is not available at milestone
  time and a loop that could see it would be tuning on the test set.
* **cost** (minimize) -- attributed USD.
* **stability** (maximize) -- a monotone inverse of the count of stability
  incidents, i.e. whether the machinery ran, independent of whether the code it
  produced was right. Provisional; see docs/fast_loop_pareto_protocol.md.

`latency` is deliberately absent. At this granularity wall-clock is dominated by
provider queueing, so it reads as noise on a frontier, and cost already carries
the "spent more" signal.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from orchestra.control.fast_loop.objectives import MilestoneObjective
from orchestra.control.fast_loop.schemas import CandidateRecord, CandidateStatus
from orchestra.control.pareto.dominance import dominates
from orchestra.control.pareto.schemas import ObjectiveDirection

QUALITY = "quality"
COST = "cost"
STABILITY = "stability"

OBJECTIVE_DIRECTIONS: Mapping[str, ObjectiveDirection] = {
    QUALITY: ObjectiveDirection.MAXIMIZE,
    COST: ObjectiveDirection.MINIMIZE,
    STABILITY: ObjectiveDirection.MAXIMIZE,
}

# Calibrated for candidates costing $1-2; see the protocol document for the
# derivation and for why each axis gets the size it does.
DEFAULT_EPSILON: Mapping[str, float] = {
    QUALITY: 0.02,
    COST: 0.08,
    STABILITY: 0.0,
}


#: A candidate whose backend actually ran, so its axes are measurements rather
#: than defaults. ``REJECTED`` never executed, and ``PENDING`` / ``RUNNING`` have
#: not finished; admitting either would place a point on the frontier that
#: nothing was paid to measure.
EXECUTED_STATUSES = frozenset(
    {
        CandidateStatus.VALID,
        CandidateStatus.COMMITTED,
        CandidateStatus.BACKEND_FAILED,
        CandidateStatus.HARNESS_FAILED,
        CandidateStatus.COMMIT_VALIDATION_FAILED,
    }
)

#: The candidate's own acceptance gate passed.
PASSING_STATUSES = frozenset({CandidateStatus.VALID, CandidateStatus.COMMITTED})


class SelectionRule:
    QUALITY_FIRST = "quality_first"
    BALANCED_KNEE = "balanced_knee"


@dataclass(frozen=True)
class ParetoSelectionConfig:
    """Axis tolerances and how one point is chosen off the frontier."""

    epsilon: Mapping[str, float] = field(default_factory=lambda: dict(DEFAULT_EPSILON))
    rule: str = SelectionRule.QUALITY_FIRST
    # A failing candidate can legitimately sit on the frontier -- cheap and
    # stable -- but committing it would freeze unaccepted work. Selection
    # therefore prefers passing points; the frontier itself is left honest.
    require_gate_pass: bool = True

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object] | None) -> ParetoSelectionConfig:
        if not payload:
            return cls()
        raw_eps = payload.get("epsilon")
        epsilon = dict(DEFAULT_EPSILON)
        if isinstance(raw_eps, Mapping):
            for name, value in raw_eps.items():
                if str(name) in OBJECTIVE_DIRECTIONS:
                    epsilon[str(name)] = float(value)  # type: ignore[arg-type]
        rule = str(payload.get("rule") or SelectionRule.QUALITY_FIRST)
        if rule not in {SelectionRule.QUALITY_FIRST, SelectionRule.BALANCED_KNEE}:
            raise ValueError(f"unknown fast-loop selection rule: {rule!r}")
        return cls(
            epsilon=epsilon,
            rule=rule,
            require_gate_pass=bool(payload.get("require_gate_pass", True)),
        )


def discriminating_quality(
    candidates: Sequence[CandidateRecord],
) -> dict[str, float]:
    """Quality over the behavioural tests that actually differ across the pool.

    Every candidate of one search is graded against a single frozen suite, so a test
    they all pass and a test they all fail are both constants. They still occupy the
    score's range: on the recorded imapclient search, 43 of 58 authored tests passed
    for every candidate and 7 failed for every candidate, so 50 of 58 units of the
    axis were fixed and the 8 that moved were reported at 8/58 of their size
    (EXP-20260810-05). Removing the constant part is not a reweighting -- it is
    declining to average a measurement with a constant.

    Returns an empty mapping when the pool cannot support the comparison: fewer than
    two measured candidates, a stage that reports no per-test identities, or suites
    of different sizes, which would mean the candidates were not graded against the
    same yardstick. Callers fall back to the raw behavioural score.

    When every candidate failed exactly the same tests the pool is genuinely tied on
    behaviour, and every candidate is given 1.0 -- equal, so the axis defers to cost
    and stability rather than inventing a difference.
    """
    measured = [
        c
        for c in candidates
        if c.status in EXECUTED_STATUSES and c.behaviour_total is not None
    ]
    if len(measured) < 2:
        return {}
    totals = {c.behaviour_total for c in measured}
    if len(totals) != 1:
        return {}
    total = next(iter(totals))
    if not total or total <= 0:
        return {}
    failed_sets = {c.candidate_id: set(c.behaviour_failures) for c in measured}
    # A candidate reporting no identities while its counts say tests failed has not
    # told us *which*, so the pool cannot be split into constant and varying parts.
    for cand in measured:
        implied_failures = total - round((cand.behaviour_score or 0.0) * total)
        if implied_failures > 0 and not failed_sets[cand.candidate_id]:
            return {}
    union: set[str] = set().union(*failed_sets.values()) if failed_sets else set()
    intersection: set[str] = (
        set.intersection(*failed_sets.values()) if failed_sets else set()
    )
    varying = union - intersection
    if not varying:
        return {c.candidate_id: 1.0 for c in measured}
    return {
        cid: (len(varying) - len(failed & varying)) / len(varying)
        for cid, failed in failed_sets.items()
    }


def objective_vector(candidate: CandidateRecord) -> dict[str, dict[str, object]]:
    """The candidate's three axes, each carrying whether it is known.

    An axis with no evidence is marked unavailable rather than defaulted, because
    ``dominance.dominates`` refuses to compare an incomplete vector. Unknown
    quality must not become a quality of zero.
    """
    objective = MilestoneObjective(
        milestone_id="",
        candidate_id=candidate.candidate_id,
        gate_passed=candidate.status in PASSING_STATUSES,
        harness_score=candidate.harness_score,
        gate_score=candidate.quality_score,
        prompt_tokens=candidate.cost.prompt_tokens,
        completion_tokens=candidate.cost.completion_tokens,
        estimated_cost_usd=candidate.cost.estimated_cost_usd,
        latency_ms=candidate.latency_ms,
        furthest_stage=candidate.furthest_stage,
    )
    # Prefer the behavioural slice over the blended score. The blend adds three
    # stages that every committed candidate passes outright, so it reports real
    # differences at roughly a third of their size -- enough to push them under the
    # quality epsilon and be recorded as ties (EXP-20260810-05). Falling back to
    # the blend keeps milestones whose harness reports no behavioural stage
    # comparable exactly as they were.
    quality = (
        candidate.comparable_quality
        if candidate.comparable_quality is not None
        else candidate.behaviour_score
    )
    quality_known = quality is not None
    if not quality_known:
        quality = objective.effective_score
        quality_known = (
            candidate.harness_score is not None or candidate.quality_score is not None
        )
    # `estimated_cost_usd` is a float defaulting to 0.0, not an optional, so an
    # unpriced candidate arrives here looking free -- and free dominates
    # everything. A candidate that spent tokens cannot have cost exactly zero, so
    # that combination is missing evidence rather than a measurement.
    cost = candidate.cost.estimated_cost_usd
    cost_known = cost > 0.0 or objective.total_tokens == 0
    return {
        QUALITY: {
            "value": quality if quality_known else None,
            "available": quality_known,
        },
        COST: {"value": cost, "available": cost_known},
        # Always known: no incidents recorded is a real observation of zero, not
        # missing evidence.
        STABILITY: {
            "value": -float(len(candidate.stability_incidents)),
            "available": True,
        },
    }


def frontier(
    candidates: Sequence[CandidateRecord],
    config: ParetoSelectionConfig | None = None,
) -> list[CandidateRecord]:
    """The mutually non-dominating candidates, in generation order.

    Only candidates that actually ran are considered: a candidate rejected before
    execution has no measured axes, and admitting it would put a point on the
    frontier that nothing was ever paid to measure.
    """
    cfg = config or ParetoSelectionConfig()
    directions = {name: d.value for name, d in OBJECTIVE_DIRECTIONS.items()}
    ran = [c for c in candidates if c.status in EXECUTED_STATUSES]
    vectors = {c.candidate_id: objective_vector(c) for c in ran}
    kept: list[CandidateRecord] = []
    for cand in ran:
        mine = vectors[cand.candidate_id]
        if any(
            dominates(vectors[other.candidate_id], mine, directions, cfg.epsilon)
            for other in ran
            if other.candidate_id != cand.candidate_id
            and _may_dominate(other, cand)
        ):
            continue
        kept.append(cand)
    return kept


def _may_dominate(other: CandidateRecord, cand: CandidateRecord) -> bool:
    """Whether `other` is even eligible to dominate `cand`.

    Failing the acceptance gate is not a position on a trade-off curve, it is a
    disqualification, so a failing candidate may not push a passing one off the
    frontier. Without this a candidate that abandoned the gate and therefore
    scored high and spent little would dominate the passing work on both axes —
    and once the passing point is gone, selection has nothing safe left to prefer,
    so `require_gate_pass` silently stops protecting anything.

    The reverse is allowed: a passing candidate dominating a failing one is a
    genuine improvement on every axis that matters.
    """
    if other.status in PASSING_STATUSES:
        return True
    return cand.status not in PASSING_STATUSES


def _normalised_distance(
    candidate: CandidateRecord, points: Sequence[CandidateRecord]
) -> float:
    """Distance from the ideal corner, each axis scaled by the frontier's spread.

    Scaling per axis is what stops the axis with the largest raw numbers from
    deciding on its own; dollars and a 0..1 score are not otherwise comparable.
    """
    vectors = {c.candidate_id: objective_vector(c) for c in points}
    total = 0.0
    for name, direction in OBJECTIVE_DIRECTIONS.items():
        values = [
            float(vectors[c.candidate_id][name]["value"])  # type: ignore[arg-type]
            for c in points
            if vectors[c.candidate_id][name]["available"]
        ]
        if not values:
            continue
        low, high = min(values), max(values)
        span = high - low
        mine = vectors[candidate.candidate_id][name]
        if not mine["available"]:
            # Unknown on this axis is treated as the worst observed value, so an
            # unmeasured axis cannot flatter a candidate into winning.
            scaled = 1.0
        elif span <= 0:
            scaled = 0.0
        else:
            value = float(mine["value"])  # type: ignore[arg-type]
            best = high if direction is ObjectiveDirection.MAXIMIZE else low
            scaled = abs(best - value) / span
        total += scaled**2
    return total


def select_from_frontier(
    points: Sequence[CandidateRecord], config: ParetoSelectionConfig | None = None
) -> CandidateRecord | None:
    """Pick the one design to commit, out of several that no other beats."""
    cfg = config or ParetoSelectionConfig()
    if not points:
        return None
    pool = list(points)
    if cfg.require_gate_pass:
        passing = [c for c in pool if c.status in PASSING_STATUSES]
        if passing:
            pool = passing
    vectors = {c.candidate_id: objective_vector(c) for c in pool}

    if cfg.rule == SelectionRule.BALANCED_KNEE:
        return min(
            pool, key=lambda c: (_normalised_distance(c, pool), c.candidate_id)
        )

    def quality_key(cand: CandidateRecord) -> tuple:
        vec = vectors[cand.candidate_id]
        quality = float(vec[QUALITY]["value"]) if vec[QUALITY]["available"] else -1.0  # type: ignore[arg-type]
        cost = float(vec[COST]["value"]) if vec[COST]["available"] else float("inf")  # type: ignore[arg-type]
        stability = float(vec[STABILITY]["value"])  # type: ignore[arg-type]
        return (-quality, cost, -stability, cand.candidate_id)

    return min(pool, key=quality_key)
