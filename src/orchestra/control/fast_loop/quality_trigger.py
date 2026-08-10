"""When to search a milestone that already passed its gate.

The fast loop has only ever been reachable through failure: `ReadySubtaskScheduler`
invokes it in the branch where a milestone's gate failed, and a milestone that
commits returns before the loop is considered. That made the tuning population one
repository wide. Of the four CodeProjectEval repositories the planner splits,
imapclient is the only one whose gate fails with any regularity; pyjwt, simpy and
bplustree pass first try, so a tuning arm on them searches nothing at full price
(EXP-20260810-04).

A gate that passes is not evidence the milestone is good — it is evidence the
milestone is safe to build on. EXP-20260810-03 measured nine imapclient candidates
at exactly 1.0 on the gate with held-out rates from 0.307 to 0.375. Now that the
graded score can separate designs, "passed but scored poorly" is a searchable
condition, and this decides when to spend on it.

Two properties are not negotiable, and both are enforced where the search is run
rather than here:

* the first-pass result competes as a candidate, so a search that finds nothing
  better keeps what it had rather than adopting a worse design; and
* a candidate that fails the gate can never displace an incumbent that passed —
  quality never buys its way past safety.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from orchestra.control.fast_loop.schemas import (
    CandidateRecord,
    CandidateStatus,
    CostRecord,
    FailureDiagnosis,
)
from orchestra.control.task_state import SubtaskFailureReason
from orchestra.ir.graph import OrchestraGraph
from orchestra.ir.nodes import NodeKind

#: Reserved id for the first-pass result when it competes in a quality search.
#: Generated candidates are named after their edit, so this cannot collide.
INCUMBENT_CANDIDATE_ID = "incumbent_first_pass"


@dataclass(frozen=True)
class QualityTrigger:
    """Whether a passing milestone is worth searching, and how poor is poor.

    Off by default: switching it on multiplies the cost of every milestone that
    scores below the threshold, including on repositories where the search has
    nothing to find.
    """

    enabled: bool = False
    #: Fire when the graded score is strictly below this. 1.0 would search every
    #: milestone that is not perfect; 0.0 searches nothing.
    min_score: float = 0.0

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> QualityTrigger:
        data = dict(raw or {})
        min_score = float(data.get("min_score", 0.0))
        if not 0.0 <= min_score <= 1.0:
            raise ValueError(f"quality_trigger.min_score must be in [0, 1], got {min_score}")
        return cls(enabled=bool(data.get("enabled", False)), min_score=min_score)

    def fires(self, *, gate_passed: bool, harness_score: float | None) -> bool:
        """Whether to search a milestone whose gate has just passed.

        An unknown score never fires. The point of the trigger is to spend where a
        measurement says there is room, and "we did not measure" is not that: a
        milestone with no graded score would otherwise search on every run,
        unfalsifiably, since nothing it produced could raise the missing number.
        """
        if not self.enabled or not gate_passed:
            return False
        if harness_score is None:
            return False
        return harness_score < self.min_score


def build_incumbent_record(
    *,
    attempt_id: int,
    graph_hash: str,
    harness_score: float | None,
    furthest_stage: str = "",
    cost: CostRecord | None = None,
    latency_ms: int | None = None,
) -> CandidateRecord:
    """The first-pass result, expressed as a candidate so it can win.

    A quality search starts from work that already passed its gate, so the search
    must be able to conclude "nothing beat it". Without the incumbent on the
    frontier the selector would choose the best of the *alternatives* and commit
    it, which on a degenerate frontier means paying three candidates to replace a
    passing milestone with a cheaper, worse one.

    It is marked ``VALID`` because that is what it is — a run whose gate passed —
    and its cost is the first pass's real spend, so a candidate has to be better
    on some axis rather than merely cheaper than a phantom zero.
    """
    return CandidateRecord(
        candidate_id=INCUMBENT_CANDIDATE_ID,
        attempt_id=attempt_id,
        graph_hash=graph_hash,
        parent_graph_hash=graph_hash,
        edits=[],
        status=CandidateStatus.VALID,
        quality_score=1.0,
        harness_score=harness_score,
        furthest_stage=furthest_stage,
        cost=cost or CostRecord(),
        latency_ms=latency_ms,
        metadata={
            "incumbent": True,
            "generation_reason": "first-pass result, entered so the search can decline",
            "no_graph_edit": True,
        },
    )


def gate_feeding_agent(graph: OrchestraGraph) -> str | None:
    """The agent whose output the acceptance gate scored.

    In a quality search no node failed, so there is no failed node to edit. The
    work being improved is whatever the gate measured, which is the agent
    immediately upstream of the harness — falling back to the last agent in the
    graph when the harness has no agent edge into it.
    """
    harness_ids = {n.node_id for n in graph.nodes if n.node_kind is NodeKind.HARNESS}
    agents = [n.node_id for n in graph.nodes if n.node_kind is NodeKind.AGENT]
    if not agents:
        return None
    feeding = [
        edge.source_node
        for edge in graph.edges
        if edge.destination_node in harness_ids and edge.source_node in set(agents)
    ]
    if feeding:
        return feeding[-1]
    return agents[-1]


def quality_search_diagnosis(
    incumbent: CandidateRecord, graph: OrchestraGraph | None = None
) -> FailureDiagnosis:
    """The 'diagnosis' for a milestone that passed but scored poorly.

    Candidate generation is driven by a diagnosis, and there is no failure to
    diagnose here: the gate passed. What the generator needs is the stage that lost
    the score and a node to attach an edit to, which are the same two things a
    failure would have supplied, so a quality search reuses the mechanism rather
    than fabricating a failure. No node is reported as failed, because none was.
    """
    score = incumbent.harness_score
    return FailureDiagnosis(
        reason=SubtaskFailureReason.HARNESS,
        retryable=True,
        infrastructure_related=False,
        concise_feedback=(
            "The acceptance gate passed, so this milestone is safe to build on, but "
            f"its graded score was {score if score is not None else 'unknown'}: parts "
            "of the milestone's own acceptance evidence did not pass. Improve the "
            f"weakest stage ({incumbent.furthest_stage or 'unknown'}) without "
            "regressing what already passes."
        ),
        furthest_stage=incumbent.furthest_stage,
        focus_node_id=gate_feeding_agent(graph) if graph is not None else None,
    )
