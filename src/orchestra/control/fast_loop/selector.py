"""Deterministic Fast Loop candidate selector (not Pareto)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

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


class DeterministicCandidateSelector:
    """Select a single winner by quality → cost → stability → latency → id."""

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

        def sort_key(c: CandidateRecord) -> tuple:
            quality = -(c.quality_score or 0.0)
            cost = c.cost.estimated_cost_usd
            tokens = c.cost.prompt_tokens + c.cost.completion_tokens
            incidents = len(c.stability_incidents)
            latency = c.latency_ms if c.latency_ms is not None else 10**12
            return (quality, cost, tokens, incidents, latency, c.candidate_id)

        return sorted(eligible, key=sort_key)[0]
