"""Canonical Pareto candidate construction."""

from __future__ import annotations

import hashlib
import json

from orchestra.control.pareto.schemas import ParetoDecisionContext, ParetoOrchestraCandidate
from orchestra.control.slow_loop.schemas import GlobalCandidate, GlobalEdit


def edit_signature(edits: list[GlobalEdit]) -> str:
    payload = [edit.model_dump(mode="json") for edit in edits]
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def candidate_content_hash(global_candidate: GlobalCandidate) -> str:
    payload = {
        "edits": [e.model_dump(mode="json") for e in global_candidate.edits],
        "plan": global_candidate.proposed_task_plan.model_dump(mode="json"),
        "communication": global_candidate.proposed_communication_plan.model_dump(mode="json"),
        "scheduling": global_candidate.proposed_scheduling_policy.model_dump(mode="json"),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str, separators=(",", ":")).encode()
    ).hexdigest()


def build_pareto_candidate(
    *,
    global_candidate: GlobalCandidate,
    context: ParetoDecisionContext,
    communication_overhead: float = 0.0,
) -> ParetoOrchestraCandidate:
    content_hash = candidate_content_hash(global_candidate)
    return ParetoOrchestraCandidate(
        candidate_id=global_candidate.candidate_id,
        content_hash=content_hash,
        edit_signature=edit_signature(list(global_candidate.edits)),
        context_id=context.context_id,
        edits=list(global_candidate.edits),
        global_candidate=global_candidate,
        communication_overhead=communication_overhead,
    )
