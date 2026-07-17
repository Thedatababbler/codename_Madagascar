"""Deterministic candidate selector (not Pareto)."""

from __future__ import annotations

from orchestra.control.fast_loop.schemas import (
    CandidateRecord,
    CandidateStatus,
    CostRecord,
    FastLoopBudget,
    StabilityIncident,
)
from orchestra.control.fast_loop.selector import DeterministicCandidateSelector


def _cand(
    cid: str,
    *,
    status: CandidateStatus = CandidateStatus.VALID,
    quality: float | None = 1.0,
    cost: float = 0.1,
    incidents: int = 0,
    latency: int = 100,
) -> CandidateRecord:
    return CandidateRecord(
        candidate_id=cid,
        attempt_id=1,
        graph_hash=f"h-{cid}",
        parent_graph_hash="parent",
        edits=[],
        status=status,
        quality_score=quality,
        cost=CostRecord(estimated_cost_usd=cost, backend_calls=1),
        stability_incidents=[
            StabilityIncident(kind="x", message="m") for _ in range(incidents)
        ],
        latency_ms=latency,
    )


def test_selector_prefers_higher_quality():
    winner = DeterministicCandidateSelector().select(
        [_cand("a", quality=0.5), _cand("b", quality=1.0)],
        FastLoopBudget(),
    )
    assert winner is not None
    assert winner.candidate_id == "b"


def test_selector_breaks_ties_by_id():
    winner = DeterministicCandidateSelector().select(
        [_cand("b", cost=0.1), _cand("a", cost=0.1)],
        FastLoopBudget(),
    )
    assert winner is not None
    assert winner.candidate_id == "a"


def test_selector_excludes_harness_failed():
    winner = DeterministicCandidateSelector().select(
        [_cand("x", status=CandidateStatus.HARNESS_FAILED, quality=0.0)],
        FastLoopBudget(),
    )
    assert winner is None
