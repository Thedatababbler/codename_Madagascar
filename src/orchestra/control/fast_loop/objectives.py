"""What the fast loop optimises when a milestone finishes.

The fast loop reruns a *single* milestone as variant candidates and picks one.
Its old selector ordered them by a 0/1 quality score, then cost, then tokens --
which means that until a candidate passed outright, every candidate looked
identical on the axis that mattered and the tie was broken by price. The
cheapest failure won.

Three axes replace it, all measurable the moment a milestone's gate has run and
none of them requiring the held-out suite:

* **gate** -- did this milestone's acceptance gate pass. Still the first thing
  that matters; a passing candidate must never lose to a failing one.
* **harness score** -- how far a candidate got, graded by stage. This is what
  gives the loop somewhere to climb between two failures.
* **tokens** -- what the attempt cost. Only ever a tie-breaker: buying a pass
  with more tokens is the trade this experiment is trying to measure, not one to
  optimise away.

Deliberately absent is the hidden pass rate. It is not available at milestone
time, and a loop that could see it would be tuning on the test set.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

DEFAULT_GATE_WEIGHT = 1.0
DEFAULT_HARNESS_WEIGHT = 0.5
DEFAULT_TOKEN_WEIGHT = 0.05


@dataclass(frozen=True)
class MilestoneObjective:
    """One milestone attempt, scored on the three fast-loop axes."""

    milestone_id: str
    candidate_id: str
    gate_passed: bool
    harness_score: float | None
    # What the gate itself scored, for harnesses that report no stage progress.
    # Usually 1.0 or 0.0, since a gate is a verdict.
    gate_score: float | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    estimated_cost_usd: float | None = None
    latency_ms: int | None = None
    furthest_stage: str = ""
    stages: list[dict[str, Any]] = field(default_factory=list)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def effective_score(self) -> float:
        """Harness score, or the gate itself when the harness reports none.

        A plain pytest harness prints no progress line. Treating that absence as
        zero would rank a passing candidate below a partially-failing one that
        happened to run under a harness that does report.
        """
        if self.harness_score is not None:
            return self.harness_score
        if self.gate_score is not None:
            return self.gate_score
        return 1.0 if self.gate_passed else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "milestone_id": self.milestone_id,
            "candidate_id": self.candidate_id,
            "gate_passed": self.gate_passed,
            "harness_score": self.harness_score,
            "effective_score": self.effective_score,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "estimated_cost_usd": self.estimated_cost_usd,
            "latency_ms": self.latency_ms,
            "furthest_stage": self.furthest_stage,
            "stages": list(self.stages),
        }


@dataclass(frozen=True)
class TuningWeights:
    """How the three axes trade off against each other.

    ``token_weight`` is applied to tokens normalised by ``token_reference``, so
    it stays comparable across milestones of very different size. It is small on
    purpose: at the default, a candidate has to spend a whole reference budget
    more to give up 0.05 of score.
    """

    gate_weight: float = DEFAULT_GATE_WEIGHT
    harness_weight: float = DEFAULT_HARNESS_WEIGHT
    token_weight: float = DEFAULT_TOKEN_WEIGHT
    token_reference: int = 1_000_000
    # With this off, a passing candidate cannot lose to a failing one whatever
    # it costs. Turning it on lets a very cheap near-miss outrank an expensive
    # pass, which is a legitimate thing to search for and a terrible default.
    allow_cost_to_outrank_gate: bool = False

    def utility(self, objective: MilestoneObjective) -> float:
        gate = self.gate_weight if objective.gate_passed else 0.0
        harness = self.harness_weight * objective.effective_score
        reference = max(1, self.token_reference)
        penalty = self.token_weight * (objective.total_tokens / reference)
        return gate + harness - penalty


def milestone_objectives(state: Any) -> list[MilestoneObjective]:
    """One objective row per milestone of a finished run.

    Recorded even when the fast loop is switched off, because the point of the
    axes is that they are known the moment a milestone's gate has run. Writing
    them out unconditionally means a tuning loop can be calibrated against runs
    that were not themselves tuned.
    """
    rows: list[MilestoneObjective] = []
    usage_by_subtask: dict[str, tuple[int, int, float | None]] = {}
    for record in getattr(state, "backend_usage_records", None) or []:
        subtask_id = str(getattr(record, "subtask_id", "") or "")
        prompt, completion, cost = usage_by_subtask.get(subtask_id, (0, 0, None))
        record_cost = getattr(record, "estimated_cost_usd", None)
        usage_by_subtask[subtask_id] = (
            prompt + int(getattr(record, "prompt_tokens", 0) or 0),
            completion + int(getattr(record, "completion_tokens", 0) or 0),
            cost if record_cost is None else (cost or 0.0) + float(record_cost),
        )

    for subtask_id, sub in (getattr(state, "subtasks", None) or {}).items():
        prompt, completion, cost = usage_by_subtask.get(subtask_id, (0, 0, None))
        fast_loop_state = (getattr(state, "fast_loop_states", None) or {}).get(subtask_id)
        harness_score = None
        stage = ""
        for candidate in getattr(fast_loop_state, "candidates", None) or []:
            score = getattr(candidate, "harness_score", None)
            if score is not None and (harness_score is None or score > harness_score):
                harness_score = score
                stage = getattr(candidate, "furthest_stage", "") or ""
        rows.append(
            MilestoneObjective(
                milestone_id=subtask_id,
                candidate_id=getattr(fast_loop_state, "winner_candidate_id", "") or "main",
                gate_passed=str(getattr(sub.status, "value", sub.status)) == "committed",
                harness_score=harness_score,
                prompt_tokens=prompt,
                completion_tokens=completion,
                estimated_cost_usd=cost,
                furthest_stage=stage,
            )
        )
    return rows


def rank(
    objectives: list[MilestoneObjective], weights: TuningWeights | None = None
) -> list[MilestoneObjective]:
    """Best first.

    Unless explicitly allowed, gate outcome is a hard partition applied before
    the weighted utility: no amount of cheapness promotes a failing candidate
    above a passing one.
    """
    scheme = weights or TuningWeights()

    def key(objective: MilestoneObjective) -> tuple:
        passed = 0 if objective.gate_passed else 1
        utility = -scheme.utility(objective)
        if scheme.allow_cost_to_outrank_gate:
            return (utility, objective.total_tokens, objective.candidate_id)
        return (passed, utility, objective.total_tokens, objective.candidate_id)

    return sorted(objectives, key=key)
