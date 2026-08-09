"""Deterministic Fast Loop candidate selector (not Pareto)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from orchestra.control.fast_loop.objectives import (
    MilestoneObjective,
    TuningWeights,
    rank,
)
from orchestra.control.fast_loop.schemas import (
    CandidateRecord,
    CandidateStatus,
    FastLoopBudget,
)
from orchestra.control.task_state import SubtaskFailureReason


class CandidateSelector(Protocol):
    def select(
        self,
        candidates: Sequence[CandidateRecord],
        budget: FastLoopBudget,
    ) -> CandidateRecord | None: ...


def _objective(candidate: CandidateRecord) -> MilestoneObjective:
    return MilestoneObjective(
        milestone_id="",
        candidate_id=candidate.candidate_id,
        gate_passed=candidate.status is CandidateStatus.VALID,
        harness_score=candidate.harness_score,
        gate_score=candidate.quality_score,
        prompt_tokens=candidate.cost.prompt_tokens,
        completion_tokens=candidate.cost.completion_tokens,
        estimated_cost_usd=candidate.cost.estimated_cost_usd,
        latency_ms=candidate.latency_ms,
        furthest_stage=candidate.furthest_stage,
    )


class DeterministicCandidateSelector:
    """Pick a winner by gate, then how far it got, then what it cost.

    The previous ordering was quality → cost → tokens, where quality was 0/1.
    Among candidates that had not yet passed -- which is every candidate the
    fast loop sees until one succeeds -- that first key was constant, so the
    winner was simply the cheapest failure. Grading the harness gives the middle
    term something to say.
    """

    def __init__(self, weights: TuningWeights | None = None) -> None:
        self.weights = weights or TuningWeights()

    def select(
        self,
        candidates: Sequence[CandidateRecord],
        budget: FastLoopBudget,
    ) -> CandidateRecord | None:
        del budget  # budget already enforced before/during execution
        eligible: list[CandidateRecord] = []
        for cand in candidates:
            if cand.status is not CandidateStatus.VALID:
                continue
            if cand.failure_reason is SubtaskFailureReason.INFRA:
                continue
            if cand.quality_score is None:
                continue
            eligible.append(cand)

        if not eligible:
            return None

        by_id = {cand.candidate_id: cand for cand in eligible}
        ordered = rank([_objective(cand) for cand in eligible], self.weights)
        best = by_id[ordered[0].candidate_id]

        # Stability and latency never entered the weighted utility -- both are
        # properties of the run rather than of the result -- so they stay here
        # as tie-breakers among candidates the objectives cannot separate.
        tied = [
            cand
            for cand in eligible
            if self.weights.utility(_objective(cand))
            == self.weights.utility(_objective(best))
        ]
        if len(tied) > 1:
            tied.sort(
                key=lambda c: (
                    len(c.stability_incidents),
                    c.latency_ms if c.latency_ms is not None else 10**12,
                    c.candidate_id,
                )
            )
            return tied[0]
        return best
